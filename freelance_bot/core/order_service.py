"""Order intake service shared by Telegram and future marketplace adapters."""

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from freelance_bot.core.filtering import classify, matches, telegram_order_gate
from freelance_bot.core.models import Order, SearchProfile, TelegramMessageEnvelope


_TELEGRAM_POST_URL = re.compile(
    r"(?:https?://)?(?:www\.)?t\.me/(?:c/\d+/\d+|[A-Za-z0-9_]{5,32}/\d+)(?:\?[^\s<>()]*)?",
    re.IGNORECASE,
)
_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u2060\ufeff]")


@dataclass(slots=True)
class IntakeResult:
    order: Order | None
    is_new: bool
    matched: bool
    drop_reason: str = ""


class OrderService:
    def __init__(self, database, profile: SearchProfile | None = None) -> None:
        self.database = database
        self.profile = profile or SearchProfile("default", "Все заказы")
        self._backfill_recent_content_fingerprints()

    def set_profile(self, profile: SearchProfile) -> None:
        self.profile = profile

    def intake_telegram_post(self, payload: dict) -> IntakeResult:
        """Keep Bot API compatibility by adapting its payload into common Telegram facts."""
        chat = payload["chat"]
        message = payload["message"]
        sender_user = message.get("from") or {}
        sender_chat = message.get("sender_chat") or {}
        sender_user_id = sender_user.get("id")
        if sender_chat:
            author_entity_type = "sender_chat"
        elif sender_user.get("is_deleted"):
            author_entity_type = "deleted_user"
        elif sender_user.get("is_bot"):
            author_entity_type = "bot"
        elif isinstance(sender_user_id, int) and not isinstance(sender_user_id, bool) and sender_user_id > 0:
            author_entity_type = "user"
        elif str(chat.get("type") or "").casefold() == "channel":
            author_entity_type = "channel"
        else:
            author_entity_type = ""
        author_name = " ".join(
            part for part in (str(sender_user.get("first_name") or ""), str(sender_user.get("last_name") or "")) if part
        ) or str(message.get("author_signature") or sender_chat.get("title") or "")
        return self.intake_telegram_message(TelegramMessageEnvelope(
            str(chat.get("id", "unknown")), str(message.get("message_id", "unknown")), str(payload.get("text") or ""),
            str(chat.get("title") or ""), str(chat.get("type") or ""), str(chat.get("username") or ""),
            self._telegram_datetime(message.get("date")), sender_user_id,
            str(sender_user.get("username") or ""), author_name, bool(sender_chat), author_entity_type,
        ))

    def intake_telegram_message(self, message: TelegramMessageEnvelope) -> IntakeResult:
        """Classify and persist one Telegram message from Bot API or User API."""
        original_text = message.text
        chat_id = message.chat_id
        message_id = message.message_id
        external_id = f"{chat_id}:{message_id}"
        if self.database.is_deleted_telegram_message(external_id):
            self.database.log("telegram", "skipped_deleted_order", "skipped", external_id)
            return IntakeResult(None, False, False, "skipped_deleted_order")
        # Общий шлюз для Bot API и User API отбрасывает шум до записи заказа и отправки карточки.
        gate = telegram_order_gate(
            original_text,
            self.profile.telegram_hard_stops if self.profile.telegram_gate_configured else None,
            self.profile.telegram_order_signals if self.profile.telegram_gate_configured else None,
        )
        if not gate.passed:
            # The reason is controlled locally and contains no post text or credentials.
            self.database.log(f"telegram:{message.chat_id}", "telegram_order_gate", "dropped", gate.reason)
            return IntakeResult(None, False, False, gate.reason)
        text = original_text.strip()
        order_id = hashlib.sha256(f"telegram:{external_id}".encode()).hexdigest()[:32]
        metadata, url = self._telegram_metadata(message)
        title, _, description = text.partition("\n")
        published_at = message.published_at
        budget, currency = self._extract_budget(text)
        fingerprint = self.telegram_content_fingerprint(original_text)
        result = classify(Order(
            order_id, "telegram", external_id, title[:200], description or title, url,
            budget=budget, currency=currency, published_at=published_at,
            last_checked_at=datetime.now(timezone.utc), metadata=metadata,
        ), self.profile)
        order = Order(
            order_id, "telegram", external_id, title[:200], description or title, url,
            budget=budget, currency=currency, published_at=published_at,
            last_checked_at=datetime.now(timezone.utc), metadata=metadata,
            classification=result.classification,
            content_fingerprint=fingerprint,
        )
        canonical, is_new = self.database.save_telegram_order(
            order, fingerprint, datetime.now(timezone.utc) - timedelta(days=7),
        )
        if canonical is None:
            # A deletion may race the preflight check; the repository repeats the
            # tombstone check inside the same transaction that would insert Order.
            self.database.log("telegram", "skipped_deleted_order", "skipped", external_id)
            return IntakeResult(None, False, False, "skipped_deleted_order")
        return IntakeResult(canonical, is_new, matches(canonical, self.profile))

    @staticmethod
    def telegram_content_fingerprint(text: str) -> str:
        """Hash minimally normalized Telegram text for exact cross-channel repost detection."""
        normalized = unicodedata.normalize("NFKC", str(text)).casefold()
        # Strip only a standalone or trailing channel-post permalink. Links in
        # the body and t.me/user contacts can be meaningful order details.
        normalized = re.sub(rf"(?m)^[ \t]*{_TELEGRAM_POST_URL.pattern}[ \t]*$", " ", normalized)
        normalized = re.sub(rf"[ \t]+{_TELEGRAM_POST_URL.pattern}[ \t]*$", "", normalized)
        normalized = _ZERO_WIDTH.sub("", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else ""

    def _backfill_recent_content_fingerprints(self) -> None:
        """Backfill hashes for recent Telegram posts already stored before this feature."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        for order in self.database.telegram_orders_missing_fingerprint(cutoff):
            metadata = order.metadata or {}
            text = str(metadata.get("original_text") or "")
            if not text and metadata.get("chat_id") and metadata.get("message_id"):
                payload = self.database.telegram_payload_for_message(
                    str(metadata["chat_id"]), str(metadata["message_id"]),
                )
                if payload:
                    text = str(payload.get("text") or "")
            if not text:
                text = "\n".join(part for part in (order.title, order.description) if part)
            fingerprint = self.telegram_content_fingerprint(text)
            if fingerprint:
                self.database.save_telegram_order(order, fingerprint, cutoff)

    def recover_telegram_metadata(self) -> int:
        """Backfill link and contact metadata only from retained durable Telegram updates."""
        updated = 0
        for order in self.database.list_orders(10_000, status=None):
            if order.platform_id != "telegram":
                continue
            old = dict(order.metadata or {})
            chat_id, message_id = str(old.get("chat_id") or ""), str(old.get("message_id") or "")
            if not chat_id or not message_id:
                continue
            payload = self.database.telegram_payload_for_message(chat_id, message_id)
            if payload is None:
                continue
            message = payload.get("message") or {}
            chat = payload.get("chat") or {}
            sender_user = message.get("from") or {}
            sender_chat = message.get("sender_chat") or {}
            sender_user_id = sender_user.get("id")
            if sender_chat:
                author_entity_type = "sender_chat"
            elif sender_user.get("is_deleted"):
                author_entity_type = "deleted_user"
            elif sender_user.get("is_bot"):
                author_entity_type = "bot"
            elif isinstance(sender_user_id, int) and not isinstance(sender_user_id, bool) and sender_user_id > 0:
                author_entity_type = "user"
            elif str(chat.get("type") or "").casefold() == "channel":
                author_entity_type = "channel"
            else:
                author_entity_type = ""
            author_name = " ".join(
                part for part in (str(sender_user.get("first_name") or ""), str(sender_user.get("last_name") or "")) if part
            ) or str(message.get("author_signature") or sender_chat.get("title") or "")
            envelope = TelegramMessageEnvelope(
                str(chat.get("id", chat_id)), str(message.get("message_id", message_id)),
                str(payload.get("text") or old.get("original_text") or ""), str(chat.get("title") or ""),
                str(chat.get("type") or ""), str(chat.get("username") or ""), self._telegram_datetime(message.get("date")),
                sender_user_id, str(sender_user.get("username") or ""), author_name, bool(sender_chat), author_entity_type,
            )
            fresh, url = self._telegram_metadata(envelope)
            # Retained Bot API input is the only trusted source for old fields;
            # it avoids deriving a customer's contact from arbitrary post text.
            # A transport can omit sender metadata; preserve earlier confirmed
            # facts in that case, while a definitive sender_chat/channel/deleted
            # classification still replaces the older author kind.
            merged = {**old, **{
                key: value for key, value in fresh.items()
                if value not in (None, "", [], {})
            }}
            if merged != old or (url and url != order.url):
                self.database.update_order_telegram_metadata(order.id, merged, url or order.url)
                updated += 1
        return updated

    @classmethod
    def _telegram_metadata(cls, message: TelegramMessageEnvelope) -> tuple[dict, str]:
        """Extract only Bot API facts and explicitly declared contact details."""
        chat_id = message.chat_id
        message_id = message.message_id
        username = message.chat_username.lstrip("@")
        declared_username = cls._declared_contact_username(message.text)
        url = cls._telegram_message_url(message.chat_id, message.chat_type, username, message_id)
        author_user_id = message.author_user_id
        valid_author_id = (
            isinstance(author_user_id, int) and not isinstance(author_user_id, bool) and author_user_id > 0
        )
        # A populated author_user_id on a transport-neutral envelope is itself
        # an adapter-confirmed Telegram User fact; adapters reject chat peers.
        author_entity_type = message.author_entity_type or (
            "user" if valid_author_id and not message.author_is_anonymous else ""
        )
        author_username = message.author_username.lstrip("@") if author_entity_type == "user" else ""
        is_contactable_user = author_entity_type == "user" and not message.author_is_anonymous
        author_contact_url = ""
        if is_contactable_user and re.fullmatch(r"[A-Za-z0-9_]{5,32}", author_username):
            author_contact_url = f"https://t.me/{author_username}"
        elif is_contactable_user and valid_author_id:
            author_contact_url = f"tg://user?id={author_user_id}"
        return {
            "chat_id": chat_id,
            "source_name": message.chat_title or username or chat_id,
            "source_type": message.chat_type,
            "source_username": username,
            "author_name": message.author_name,
            "author_username": author_username,
            "author_user_id": author_user_id if valid_author_id else None,
            "author_is_anonymous": message.author_is_anonymous,
            "author_entity_type": author_entity_type,
            "author_contact_url": author_contact_url,
            "declared_contact_username": declared_username,
            "declared_contact_url": f"https://t.me/{declared_username}" if declared_username else "",
            "message_id": message_id,
            "original_message_url": url,
            "original_text": message.text,
        }, url

    @staticmethod
    def _telegram_message_url(chat_id: str, chat_type: str, username: str, message_id: str) -> str:
        """Build a direct Telegram post URL only for formats Telegram documents."""
        if not message_id.isdigit():
            return ""
        if re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
            return f"https://t.me/{username}/{message_id}"
        # t.me/c is valid for private channels and supergroups whose Bot API ID
        # starts with -100. It still requires the viewer to be a member.
        if chat_type in {"channel", "supergroup"} and re.fullmatch(r"-100\d+", chat_id):
            return f"https://t.me/c/{chat_id[4:]}/{message_id}"
        return ""

    @staticmethod
    def _declared_contact_username(text: str) -> str:
        """Accept an @username only next to an explicit request to contact that person."""
        pattern = re.compile(
            r"(?:контакт(?:ы)?|для\s+связи|связь|пишите|писать|обращаться|личн(?:ые|ую)\s+сообщени\w*|лс|личк\w*)"
            r"[^@\n]{0,80}@([A-Za-z0-9_]{5,32})\b",
            re.IGNORECASE,
        )
        match = pattern.search(text)
        return match.group(1) if match else ""

    @staticmethod
    def _telegram_datetime(timestamp: object) -> datetime | None:
        if not isinstance(timestamp, (int, float)):
            return None
        return datetime.fromtimestamp(timestamp, timezone.utc)

    @staticmethod
    def _extract_budget(text: str) -> tuple[float | None, str]:
        """Read a simple explicit budget; ambiguous numbers remain unclassified."""
        match = re.search(r"(?<!\w)([\d][\d\s]{1,12})(₽|руб\.?|RUB|\$|USD|€|EUR)(?!\w)", text, re.IGNORECASE)
        if not match:
            return None, ""
        value = float(match.group(1).replace(" ", ""))
        symbol = match.group(2).lower()
        currency = "RUB" if symbol in {"₽", "руб", "руб.", "rub"} else "USD" if symbol in {"$", "usd"} else "EUR"
        return value, currency
