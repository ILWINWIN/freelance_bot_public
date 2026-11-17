"""Sequential dispatch queue with per-account rate limits."""

import random
from datetime import datetime, timedelta, timezone
from threading import Lock
from freelance_bot.core.models import DispatchJob, JobStatus


class DispatchQueue:
    def __init__(self, database=None) -> None:
        self._jobs: dict[str, DispatchJob] = {}
        self._last_run: dict[str, datetime] = {}
        self._lock = Lock()
        self.database = database

    def enqueue(self, job: DispatchJob) -> DispatchJob:
        with self._lock:
            self._jobs[job.id] = job
            if self.database:
                self.database.save_job(job)
        return job

    def pending(self, now: datetime | None = None) -> list[DispatchJob]:
        now = now or datetime.now(timezone.utc)
        with self._lock:
            return sorted(
                (j for j in self._jobs.values() if j.status == JobStatus.PENDING and j.next_attempt_at <= now),
                key=lambda j: j.next_attempt_at,
            )

    def can_run(self, job: DispatchJob, rate_limit) -> bool:
        last = self._last_run.get(job.account_id)
        if last is None:
            return True
        return datetime.now(timezone.utc) - last >= timedelta(seconds=rate_limit.min_delay_seconds)

    def mark_running(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.status = JobStatus.RUNNING
            job.started_at = datetime.now(timezone.utc)
            self._last_run[job.account_id] = job.started_at
            if self.database:
                self.database.save_job(job)

    def mark_awaiting_confirmation(self, job_id: str) -> None:
        self.mark_result(job_id, JobStatus.AWAITING_CONFIRMATION)

    def mark_sending(self, job_id: str) -> None:
        self.mark_result(job_id, JobStatus.SENDING)

    def mark_sent(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.status = JobStatus.SENT
            job.confirmed_at = datetime.now(timezone.utc)
            job.error = ""
            if self.database:
                self.database.save_job(job)

    def mark_unknown_result(self, job_id: str, error: str = "") -> None:
        self.mark_result(job_id, JobStatus.UNKNOWN_RESULT, error)

    def mark_result(self, job_id: str, status: JobStatus, error: str = "") -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.status = status
            job.error = error
            if self.database:
                self.database.save_job(job)

    def delay_job(self, job_id: str, rate_limit) -> None:
        with self._lock:
            job = self._jobs[job_id]
            delay = random.uniform(rate_limit.min_delay_seconds, rate_limit.max_delay_seconds)
            job.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
            if self.database:
                self.database.save_job(job)

    def restore(self, jobs: list[DispatchJob]) -> None:
        with self._lock:
            for job in jobs:
                self._jobs[job.id] = job

    def snapshot(self) -> list[DispatchJob]:
        with self._lock:
            return list(self._jobs.values())
