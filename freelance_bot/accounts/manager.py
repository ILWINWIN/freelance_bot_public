"""Account registry with a configurable active-account ceiling."""

from dataclasses import replace
from freelance_bot.core.models import Account


class AccountManager:
    def __init__(self, max_active: int = 50, accounts=None, database=None) -> None:
        if max_active < 1 or max_active > 50:
            raise ValueError("max_active must be between 1 and 50")
        self.max_active = max_active
        self.database = database
        self._accounts: dict[str, Account] = {}
        for account in accounts or []:
            self.add(account)

    def add(self, account: Account) -> None:
        self._accounts[account.id] = account
        if self.database:
            self.database.save_account(account)

    def upsert(self, account: Account, database=None) -> None:
        self.add(account)
        if database is not None and database is not self.database:
            database.save_account(account)

    def get(self, account_id: str) -> Account | None:
        return self._accounts.get(account_id)

    def all(self) -> list[Account]:
        return list(self._accounts.values())

    def active(self) -> list[Account]:
        enabled = [a for a in self._accounts.values() if a.enabled and not a.requires_attention]
        return enabled[: self.max_active]

    def set_enabled(self, account_id: str, enabled: bool) -> Account:
        account = self._accounts[account_id]
        updated = replace(account, enabled=enabled)
        self._accounts[account_id] = updated
        if self.database:
            self.database.save_account(updated)
        return updated

    def pause_for_attention(self, account_id: str) -> Account:
        account = self._accounts[account_id]
        updated = replace(account, requires_attention=True, enabled=False)
        self._accounts[account_id] = updated
        if self.database:
            self.database.save_account(updated)
        return updated

    def remove(self, account_id: str) -> None:
        """Forget a locally stored account after its external session was revoked."""
        self._accounts.pop(account_id, None)
        if self.database:
            self.database.delete_account(account_id)
