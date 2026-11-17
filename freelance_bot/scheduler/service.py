"""Orchestrates bounded source work without owning UI or persistence."""

from freelance_bot.accounts.manager import AccountManager
from freelance_bot.browser_automation.manager import BrowserSessionManager
from freelance_bot.core.exceptions import AuthenticationRequired, CaptchaDetected, PlatformChanged
from freelance_bot.core.models import Account, DispatchJob, JobStatus, Order, ProposalMode
from freelance_bot.scheduler.queue import DispatchQueue
from freelance_bot.sources.registry import SourceRegistry


class SchedulerService:
    def __init__(self, accounts: AccountManager, sources: SourceRegistry, queue: DispatchQueue, browser: BrowserSessionManager) -> None:
        self.accounts = accounts
        self.sources = sources
        self.queue = queue
        self.browser = browser

    def run_one(self, job: DispatchJob, order: Order | None) -> None:
        """Run a confirmed action; uncertain external outcomes stay non-retryable."""
        account = self.accounts.get(job.account_id)
        if account is None:
            self.queue.mark_result(job.id, JobStatus.FAILED, "Account not found")
            return
        if order is None:
            self.queue.mark_result(job.id, JobStatus.FAILED, "Order not found")
            return
        if job.mode == ProposalMode.DRAFT:
            self.queue.mark_awaiting_confirmation(job.id)
            return
        adapter = self.sources.get(account.platform_id)
        if not self.queue.can_run(job, account.rate_limit):
            self.queue.delay_job(job.id, account.rate_limit)
            return
        try:
            self.queue.mark_running(job.id)
            with self.browser.slot():
                adapter.open_order(order, account)
                self.queue.mark_sending(job.id)
                sent = adapter.send_proposal(job, account)
            if sent is True:
                self.queue.mark_sent(job.id)
            else:
                self.queue.mark_unknown_result(job.id, "Adapter did not confirm the send result")
        except (AuthenticationRequired, CaptchaDetected, PlatformChanged) as exc:
            self.accounts.pause_for_attention(account.id)
            self.queue.mark_result(job.id, JobStatus.PAUSED, str(exc))
        except Exception as exc:  # The next job must remain runnable.
            self.queue.mark_unknown_result(job.id, str(exc))
