"""Telethon reader for explicitly configured Telegram sources."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
import webbrowser

from freelance_bot.core.models import Account, DispatchJob, Order, TelegramMessageEnvelope, TelegramSource
from freelance_bot.sources.base import SourceAdapter
from freelance_bot.telegram_user.mapper import canonical_chat_id, envelope_from_telethon


@dataclass(frozen=True, slots=True)
class ResolvedTelegramSource:
    """A User API verification result without private session material."""

    chat_id: str
    name: str
    reference: str
    chat_type: str
    username: str = ""


class SourceAccessError(ValueError):
    """Safe source-access error suitable for the desktop and bot status views."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class TelegramUserAdapter(SourceAdapter):
    """Read selected chats through an already authorized Telethon client only."""

    platform_id = "telegram_user"

    def __init__(self, client, account: Account | None = None) -> None:
        self.client = client
        self.account = account

    def check_auth(self, account: Account) -> bool:
        """Report local authorization state without sending a message or joining chats."""
        return bool(self.client and getattr(self.client, "is_connected", lambda: False)())

    def fetch_orders(self, account: Account) -> list[Order]:
        """Realtime/history ingestion is asynchronous; scheduler polling is intentionally unused."""
        return []

    def open_order(self, order: Order, account: Account) -> None:
        """Open a verified original Telegram URL through the operating system."""
        if not order.url.startswith(("https://t.me/", "http://t.me/")):
            raise ValueError("Источник не предоставил ссылку на оригинал.")
        webbrowser.open(order.url)

    def prepare_proposal(self, job: DispatchJob, account: Account) -> None:
        """User API collection never prepares or sends responses automatically."""
        raise RuntimeError("Подготовка откликов через Telegram User API не реализована.")

    def send_proposal(self, job: DispatchJob, account: Account) -> bool:
        """Explicitly prevent automated messages to Telegram users."""
        return False

    async def resolve(self, reference: str) -> ResolvedTelegramSource:
        """Resolve only sources already readable by the authorized user account."""
        value = normalize_reference(reference)
        try:
            if value.startswith("https://t.me/+"):
                from telethon.tl.functions.messages import CheckChatInviteRequest
                result = await self.client(CheckChatInviteRequest(value.rsplit("+", 1)[1]))
                chat = getattr(result, "chat", None)
                if chat is None:
                    raise SourceAccessError("requires_membership", "Сначала вступите в этот чат обычным клиентом Telegram.")
            elif value.lstrip("-").isdigit():
                chat = await self._dialog_by_id(value)
                if chat is None:
                    raise SourceAccessError("not_found", "Чат с таким ID не найден среди доступных аккаунту диалогов.")
            else:
                chat = await self.client.get_entity(value)
            await self.client.get_messages(chat, limit=1)
        except SourceAccessError:
            raise
        except Exception as exc:
            raise SourceAccessError("forbidden", "Аккаунт не может подтвердить чтение этого источника.") from exc
        return ResolvedTelegramSource(
            self._peer_id(chat),
            str(getattr(chat, "title", "") or getattr(chat, "username", "") or value), value,
            self._chat_type(chat), str(getattr(chat, "username", "") or ""),
        )

    async def _dialog_by_id(self, chat_id: str):
        """Resolve numeric IDs only from dialogs already accessible to the account."""
        target = str(chat_id)
        async for dialog in self.client.iter_dialogs():
            entity = dialog.entity
            if self._peer_id(entity) == target or canonical_chat_id(getattr(dialog, "id", "")) == target:
                return entity
        return None

    async def history(self, source: TelegramSource, after_message_id: str = "", hours: int = 1,
                      until: datetime | None = None, limit: int = 100):
        """Yield a small newest-first window without joining or retaining media.

        The service decides whether this is startup or reconnect recovery.
        This adapter only enforces the requested time and message bounds.
        """
        entity = await self._entity_for_source(source)
        window_end = until or datetime.now(timezone.utc)
        if window_end.tzinfo is None:
            window_end = window_end.replace(tzinfo=timezone.utc)
        cutoff = window_end - timedelta(hours=hours)
        minimum = int(after_message_id) if after_message_id.isdigit() else 0
        messages = []
        async for message in self.client.iter_messages(entity, min_id=minimum):
            date = getattr(message, "date", None)
            if date is not None and date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            if not after_message_id and date is not None:
                if date > window_end:
                    continue
                if date < cutoff:
                    break
            if str(getattr(message, "message", "") or "").strip():
                messages.append(message)
                if len(messages) >= max(0, int(limit)):
                    break
        for message in reversed(messages):
            sender = await message.get_sender()
            yield envelope_from_telethon(message, entity, sender)

    async def envelope_from_event(self, event) -> TelegramMessageEnvelope | None:
        """Map a new-message event; media-only posts are outside the order scope."""
        message = event.message
        if not str(getattr(message, "message", "") or "").strip():
            return None
        chat = await event.get_chat()
        sender = await event.get_sender()
        return envelope_from_telethon(message, chat, sender)

    async def _entity_for_source(self, source: TelegramSource):
        """Restore an entity through its stable reference without attempting membership changes."""
        reference = source.reference or source.chat_id
        if reference.startswith("@"):
            return await self.client.get_entity(reference)
        if reference.startswith("https://t.me/+"):
            from telethon.tl.functions.messages import CheckChatInviteRequest
            result = await self.client(CheckChatInviteRequest(reference.rsplit("+", 1)[1]))
            chat = getattr(result, "chat", None)
            if chat is None:
                raise SourceAccessError("requires_membership", "Сначала вступите в этот чат обычным Telegram-клиентом.")
            return chat
        entity = await self._dialog_by_id(source.chat_id)
        if entity is None:
            raise SourceAccessError("not_found", "Источник больше не найден среди доступных аккаунту чатов.")
        return entity

    @staticmethod
    def _chat_type(chat: object) -> str:
        name = type(chat).__name__.casefold()
        if "channel" in name:
            return "channel" if bool(getattr(chat, "broadcast", False)) else "supergroup"
        return "group" if "chat" in name else ""

    @staticmethod
    def _peer_id(chat: object) -> str:
        """Use Telethon's marked peer ID so it matches Telegram Bot API chat IDs."""
        from telethon.utils import get_peer_id
        return canonical_chat_id(get_peer_id(chat))


def normalize_reference(value: str) -> str:
    """Validate only explicit Telegram source forms; no search or automatic joining."""
    value = value.strip()
    if value.lstrip("-").isdigit():
        return value
    if re.fullmatch(r"@[A-Za-z0-9_]{5,32}", value):
        return value
    match = re.fullmatch(r"https?://(?:www\.)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]{5,32}|\+[A-Za-z0-9_-]+)", value)
    if match:
        return f"https://t.me/{match.group(1)}" if match.group(1).startswith("+") else f"@{match.group(1)}"
    raise SourceAccessError("invalid_reference", "Поддерживаются @username, ссылка t.me или числовой chat_id.")
