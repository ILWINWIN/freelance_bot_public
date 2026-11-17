"""Telegram User API ingestion isolated from the Bot API control plane."""

from freelance_bot.telegram_user.service import TelegramUserService

__all__ = ["TelegramUserService"]
