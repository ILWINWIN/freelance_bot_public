"""Map Telethon objects into core models without importing Telethon in the core."""

from datetime import timezone

from freelance_bot.core.models import TelegramMessageEnvelope


def canonical_chat_id(value: object) -> str:
    """Keep Telegram's marked peer ID stable across Bot API and User API."""
    return str(value)


def chat_type(chat: object) -> str:
    """Return the limited chat type vocabulary used by direct-link generation."""
    name = type(chat).__name__.casefold()
    if "channel" in name:
        return "channel" if bool(getattr(chat, "broadcast", False)) else "supergroup"
    if "chat" in name:
        return "group"
    return ""


def envelope_from_telethon(message: object, chat: object, sender: object | None = None) -> TelegramMessageEnvelope:
    """Convert an already fetched Telethon message to neutral, safe source facts."""
    date = getattr(message, "date", None)
    if date is not None and date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    # Telethon may return a Channel or Chat as the sender for channel posts
    # and anonymous admins. Only its concrete User entity is a personal contact.
    sender_is_user = sender is not None and type(sender).__name__.casefold() == "user"
    sender_is_deleted = bool(getattr(sender, "deleted", False)) if sender_is_user else False
    sender_is_bot = bool(getattr(sender, "bot", False)) if sender_is_user else False
    author_entity_type = (
        "user" if sender_is_user and not sender_is_deleted and not sender_is_bot
        else "deleted_user" if sender_is_user and sender_is_deleted
        else "bot" if sender_is_user and sender_is_bot
        else "sender_chat" if sender is not None
        else "anonymous" if bool(getattr(message, "post_author", ""))
        else "channel" if chat_type(chat) == "channel"
        else ""
    )
    contactable_user = author_entity_type == "user"
    sender_id = getattr(sender, "id", None) if contactable_user else None
    username = str(getattr(sender, "username", "") or "") if contactable_user else ""
    author_name = " ".join(
        str(part) for part in (getattr(sender, "first_name", ""), getattr(sender, "last_name", "")) if part
    ) if contactable_user else ""
    author_name = author_name or str(getattr(message, "post_author", "") or "")
    return TelegramMessageEnvelope(
        canonical_chat_id(getattr(message, "chat_id", getattr(chat, "id", ""))),
        str(getattr(message, "id", "")),
        str(getattr(message, "message", "") or ""),
        str(getattr(chat, "title", "") or ""),
        chat_type(chat),
        str(getattr(chat, "username", "") or ""),
        date,
        int(sender_id) if isinstance(sender_id, int) else None,
        username,
        author_name,
        author_entity_type in {"sender_chat", "anonymous"},
        author_entity_type,
    )
