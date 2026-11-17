"""Framework-neutral Telegram service facade.

The optional python-telegram-bot integration can call these methods. Keeping
the service independent means the local core remains testable without a token.
"""

class TelegramService:
    def __init__(self, token: str = "", allowed_user_ids: set[int] | None = None, admin_user_ids: set[int] | None = None) -> None:
        self.token = token
        self.allowed_user_ids = allowed_user_ids or set()
        # Older configurations have a single owner allowlist. It remains the
        # administrator list until the optional explicit list is configured.
        self.admin_user_ids = admin_user_ids if admin_user_ids is not None else set(self.allowed_user_ids)
        self.running = False
        self.last_error = ""

    @property
    def configured(self) -> bool:
        return bool(self.token and self.allowed_user_ids)

    def authorize(self, user_id: int) -> bool:
        return user_id in self.allowed_user_ids

    def authorize_admin(self, user_id: int) -> bool:
        """Return whether an allowed user may change sources or archive orders."""
        return user_id in self.allowed_user_ids and user_id in self.admin_user_ids

    def start(self) -> None:
        self.running = self.configured
        if not self.running:
            self.last_error = "Нужны токен Telegram и хотя бы один разрешённый пользователь."

    def stop(self) -> None:
        self.running = False

    def notification_text(self, title: str, url: str, score: float, order_id: str = "") -> str:
        identifier = f"\nID: {order_id}" if order_id else ""
        return f"Новый заказ: {title}\nСоответствие: {score:.0%}{identifier}\n{url}".strip()
