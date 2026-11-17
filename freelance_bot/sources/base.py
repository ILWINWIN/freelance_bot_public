"""Stable interface implemented by every marketplace or Telegram source."""

from abc import ABC, abstractmethod
from freelance_bot.core.models import Account, DispatchJob, Order


class SourceAdapter(ABC):
    platform_id: str

    @abstractmethod
    def check_auth(self, account: Account) -> bool: ...

    @abstractmethod
    def fetch_orders(self, account: Account) -> list[Order]: ...

    @abstractmethod
    def open_order(self, order: Order, account: Account) -> None: ...

    @abstractmethod
    def prepare_proposal(self, job: DispatchJob, account: Account) -> None: ...

    @abstractmethod
    def send_proposal(self, job: DispatchJob, account: Account) -> bool:
        """Send a proposal and return True only after external confirmation."""

    def close(self, account: Account) -> None:
        """Release account-specific resources; adapters may override this."""
