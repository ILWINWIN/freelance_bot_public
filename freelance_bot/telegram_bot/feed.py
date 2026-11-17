"""Batch Telegram presentation of saved orders without a separate feed store."""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from freelance_bot.core.models import Order, OrderClassification, OrderStatus
from freelance_bot.telegram_bot.transport import TelegramCallbackResult, TelegramReply


PAGE_SIZE = 10
TELEGRAM_TEXT_LIMIT = 4096
_TELEGRAM_MESSAGE_URL = re.compile(r"https://t\.me/(?:[A-Za-z0-9_]{5,32}|c/\d+)/\d+\Z")
_CONTACT_URL = re.compile(r"https://t\.me/[A-Za-z0-9_]{5,32}\Z")
_CONTACT_ID_URL = re.compile(r"tg://user\?id=[1-9]\d*\Z")


@dataclass(frozen=True, slots=True)
class FeedCategory:
    code: str
    title: str
    status: OrderStatus
    classification: OrderClassification | None
    recent_only: bool


_CATEGORIES = {
    "m": FeedCategory("m", "Лента заказов", OrderStatus.ACTIVE, OrderClassification.MATCHED, True),
    "u": FeedCategory("u", "Не определено", OrderStatus.ACTIVE, OrderClassification.UNDETERMINED, False),
    "a": FeedCategory("a", "Архив", OrderStatus.ARCHIVED, None, False),
}


class TelegramOrderFeed:
    """Render chronological pages from the common SQLite order storage."""

    def __init__(self, database, can_archive: Callable[[int], bool] | None = None) -> None:
        self.database = database
        self.can_archive = can_archive or (lambda _user_id: False)

    def command(self, user_id: int, args: str) -> list[TelegramReply]:
        """Open a page of at most ten saved orders, newest first."""
        aliases = {"подходящие": "m", "лента": "m", "неопределено": "u", "архив": "a"}
        code = aliases.get(args.strip().lower(), args.strip().lower())
        return self._page(user_id, code if code in _CATEGORIES else "m")

    def new_order(self, user_id: int, order: Order) -> TelegramReply:
        """Render one freshly saved matching order for an owner notification."""
        return self._card(order, "m", user_id=user_id)

    def callback(self, user_id: int, data: str) -> TelegramCallbackResult | None:
        """Load another page or reveal a long original text for an existing order."""
        parts = data.split(":", 4)
        if len(parts) < 2 or parts[0] != "feed":
            return None
        action = parts[1]
        if action == "more" and len(parts) == 4:
            code, cursor_id = parts[2], parts[3]
            if code not in _CATEGORIES:
                return TelegramCallbackResult(notice="Категория больше недоступна.")
            cursor = self.database.get_order(cursor_id)
            edit = self._card(cursor, code, user_id=user_id) if cursor else None
            page = self._page(user_id, code, cursor_id)
            return TelegramCallbackResult(edit=edit, messages=page)
        if action == "archive" and len(parts) == 4:
            return TelegramCallbackResult(notice="Архив заказов доступен только в desktop-приложении.")
        if action == "text" and len(parts) == 5:
            code, order_id, part = parts[2], parts[3], parts[4]
            order = self.database.get_order(order_id)
            try:
                part_number = int(part)
            except ValueError:
                part_number = 0
            if order is None or code not in _CATEGORIES:
                return TelegramCallbackResult(notice="Объявление больше недоступно.")
            return TelegramCallbackResult(edit=self._text_part(order, code, part_number))
        if action == "card" and len(parts) == 4:
            code, order_id = parts[2], parts[3]
            order = self.database.get_order(order_id)
            if order is None or code not in _CATEGORIES:
                return TelegramCallbackResult(notice="Объявление больше недоступно.")
            return TelegramCallbackResult(edit=self._card(order, code, user_id=user_id))
        return TelegramCallbackResult(notice="Действие больше недоступно.")

    def _page(self, user_id: int, code: str, cursor_id: str = "") -> list[TelegramReply]:
        category = _CATEGORIES[code]
        cursor = self.database.get_order(cursor_id) if cursor_id else None
        before = self._sort_key(cursor) if cursor else None
        since = datetime.now(timezone.utc) - timedelta(hours=48) if category.recent_only else None
        orders = self.database.list_orders(
            PAGE_SIZE + 1,
            status=category.status,
            classification=category.classification,
            since=since,
            before=before,
            sort="published_newest" if category.recent_only else "newest",
        )
        has_more = len(orders) > PAGE_SIZE
        orders = orders[:PAGE_SIZE]
        if not orders:
            if cursor_id:
                return []
            return [TelegramReply(f"{category.title}\n\nПодходящих объявлений в этой выборке пока нет.")]
        replies = [self._card(order, code, user_id=user_id) for order in orders]
        if has_more:
            last = replies[-1]
            keyboard = dict(last.reply_markup or {})
            rows = list(keyboard.get("inline_keyboard") or [])
            rows.append([{"text": "Показать ещё", "callback_data": f"feed:more:{code}:{orders[-1].id}"}])
            keyboard["inline_keyboard"] = rows
            last.reply_markup = keyboard
        return replies

    def _card(self, order: Order | None, code: str, user_id: int = 0) -> TelegramReply | None:
        if order is None:
            return None
        metadata = order.metadata or {}
        source = str(metadata.get("source_name") or order.platform_id)
        author_name = str(metadata.get("author_name") or "").strip()
        author_username = str(metadata.get("author_username") or "").strip().lstrip("@")
        lines = ["⚡ Новое объявление", "", f"Источник: {source}"]
        if author_name or author_username:
            author = f"@{author_username}" if author_username else author_name
            if author_name and author_username:
                author = f"{author_name} (@{author_username})"
            lines.extend(["", f"Автор: {author}"])
        lines.extend(["", "Текст объявления:", "", self._main_text(order)])
        # Only the source post time is suitable here: discovery and bot-delivery
        # time would make an old post look new in the owner feed.
        if order.published_at:
            lines.extend(["", f"Опубликовано: {order.published_at.astimezone().strftime('%d.%m.%Y, %H:%M')}"])
        return TelegramReply(self._fit_text("\n".join(lines)), self._card_keyboard(order, code, user_id))

    def _card_keyboard(self, order: Order, code: str, user_id: int) -> dict | None:
        rows: list[list[dict]] = []
        links: list[dict] = []
        contact_url = self._contact_url(order)
        original_url = self._original_url(order)
        if contact_url:
            links.append({"text": "✉️ Написать", "url": contact_url})
        if original_url:
            links.append({"text": "🔗 Оригинал", "url": original_url})
        if links:
            rows.append(links)
        rows.append([{"text": "🏠 Меню", "callback_data": "menu"}])
        return {"inline_keyboard": rows} if rows else None

    def _text_part(self, order: Order, code: str, part_number: int) -> TelegramReply:
        chunks = self._text_chunks(self._original_text(order))
        part_number = max(0, min(part_number, len(chunks) - 1))
        rows: list[list[dict]] = []
        navigation = []
        if part_number:
            navigation.append({"text": "◀ Предыдущая часть", "callback_data": f"feed:text:{code}:{order.id}:{part_number - 1}"})
        if part_number + 1 < len(chunks):
            navigation.append({"text": "Следующая часть ▶", "callback_data": f"feed:text:{code}:{order.id}:{part_number + 1}"})
        if navigation:
            rows.append(navigation)
        rows.append([{"text": "↩️ К карточке", "callback_data": f"feed:card:{code}:{order.id}"}])
        return TelegramReply(
            self._fit_text(f"Продолжение текста объявления ({part_number + 1}/{len(chunks)})\n\n{chunks[part_number]}"),
            {"inline_keyboard": rows},
        )

    def _main_text(self, order: Order) -> str:
        return self._text_chunks(self._original_text(order))[0]

    def _has_more_text(self, order: Order) -> bool:
        return len(self._text_chunks(self._original_text(order))) > 1

    @staticmethod
    def _text_chunks(text: str) -> list[str]:
        """Split only when Telegram's message limit requires it, preferring line ends."""
        limit = 3500
        chunks = []
        remaining = text or ""
        while remaining:
            if len(remaining) <= limit:
                chunks.append(remaining)
                break
            cut = remaining.rfind("\n", 0, limit)
            if cut < limit // 2:
                cut = limit
            chunks.append(remaining[:cut].rstrip())
            remaining = remaining[cut:].lstrip("\n")
        return chunks or ["(Текст объявления отсутствует.)"]

    @staticmethod
    def _original_text(order: Order) -> str:
        stored = str((order.metadata or {}).get("original_text") or "")
        if stored:
            return stored
        if order.description and order.description != order.title:
            return f"{order.title}\n{order.description}"
        return order.title or order.description

    @staticmethod
    def _original_url(order: Order) -> str:
        metadata = order.metadata or {}
        url = str(metadata.get("original_message_url") or order.url or "")
        return url if order.platform_id == "telegram" and _TELEGRAM_MESSAGE_URL.fullmatch(url) else ""

    @staticmethod
    def _contact_url(order: Order) -> str:
        metadata = order.metadata or {}
        declared = str(metadata.get("declared_contact_url") or "")
        if _CONTACT_URL.fullmatch(declared):
            return declared
        url = str(metadata.get("author_contact_url") or "")
        if metadata.get("author_entity_type") != "user" or metadata.get("author_is_anonymous"):
            return ""
        valid_id = metadata.get("author_user_id")
        if isinstance(valid_id, bool) or not str(valid_id or "").isdigit() or int(valid_id or 0) <= 0:
            return ""
        return url if _CONTACT_URL.fullmatch(url) or _CONTACT_ID_URL.fullmatch(url) else ""

    @staticmethod
    def _sort_key(order: Order) -> tuple[datetime, str]:
        value = order.published_at or datetime.min.replace(tzinfo=timezone.utc)
        return value, order.id

    @staticmethod
    def _fit_text(text: str) -> str:
        return text if len(text) <= TELEGRAM_TEXT_LIMIT else text[:TELEGRAM_TEXT_LIMIT - 1] + "…"
