"""Persistent, undoable deletion of archived Orders."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Event, RLock, Thread
from time import monotonic
from uuid import uuid4


DELETION_DELAY = timedelta(minutes=5)
DELETION_CHECK_INTERVAL_SECONDS = 10


@dataclass(frozen=True, slots=True)
class DeletionScheduleResult:
    """Summarize a single or bulk scheduling request without hiding rejected IDs."""

    scheduled: int
    already_pending: int
    rejected_not_archived: int
    not_found: int
    batch_id: str
    scheduled_ids: tuple[str, ...]


class DeletionService:
    """Own deletion policy, restart recovery, runtime deadlines, and one due worker."""

    def __init__(
        self, database, *, now_fn=None, monotonic_fn=None,
        delay_seconds: float = DELETION_DELAY.total_seconds(),
    ) -> None:
        """Bind the durable store and optional test clocks; production defaults to five minutes."""
        self.database = database
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic_fn or monotonic
        self._delay_seconds = float(delay_seconds)
        self._runtime_deadlines: dict[str, float] = {}
        self._lock = RLock()
        self._worker_stop = Event()
        self._worker: Thread | None = None

    def schedule_deletion(self, order_ids, origin: str | None = None) -> DeletionScheduleResult:
        """Queue archived Orders once; repeated clicks preserve their original deadline."""
        values = (order_ids,) if isinstance(order_ids, str) else order_ids
        unique_ids = tuple(dict.fromkeys(str(value) for value in values if str(value)))
        resolved_origin = origin or ("manual_single" if len(unique_ids) <= 1 else "manual_bulk")
        if resolved_origin not in {"manual_single", "manual_bulk", "auto_cleanup"}:
            raise ValueError("Недопустимый источник запроса на удаление.")
        requested_at = self._utc(self._now())
        batch_id = uuid4().hex
        delete_after = requested_at + timedelta(seconds=self._delay_seconds)
        with self._lock:
            result = self.database.schedule_order_deletions(
                list(unique_ids), requested_at, delete_after, batch_id, resolved_origin,
            )
            runtime_deadline = self._monotonic() + self._delay_seconds
            for order_id in result["scheduled_ids"]:
                self._runtime_deadlines[str(order_id)] = runtime_deadline
        scheduled_ids = tuple(str(value) for value in result["scheduled_ids"])
        if int(result["scheduled"]):
            self.database.log(
                "system", "order_deletion_scheduled", "ok",
                f"count={result['scheduled']} batch_id={batch_id} origin={resolved_origin}",
            )
        return DeletionScheduleResult(
            int(result["scheduled"]), int(result["already_pending"]),
            int(result["rejected_not_archived"]), int(result["not_found"]),
            batch_id, scheduled_ids,
        )

    def cancel_deletion(self, order_ids) -> int:
        """Cancel queued requests by stable Order IDs and retain their archived rows."""
        values = (order_ids,) if isinstance(order_ids, str) else order_ids
        ids = tuple(dict.fromkeys(str(value) for value in values if str(value)))
        with self._lock:
            cancelled = self.database.cancel_order_deletions(list(ids))
            for order_id in ids:
                self._runtime_deadlines.pop(order_id, None)
        if cancelled:
            self.database.log("system", "order_deletion_cancelled", "ok", f"count={cancelled}")
        return cancelled

    def cancel_deletion_batch(self, batch_id: str) -> int:
        """Cancel every remaining queued request in one known bulk batch."""
        with self._lock:
            rows = self.database.list_order_deletions()
            ids = [str(row["order_id"]) for row in rows if row["batch_id"] == str(batch_id)]
            cancelled = self.database.cancel_order_deletion_batch(str(batch_id))
            for order_id in ids:
                self._runtime_deadlines.pop(order_id, None)
        if cancelled:
            self.database.log("system", "order_deletion_batch_cancelled", "ok", f"count={cancelled}")
        return cancelled

    def list_pending_deletions(self) -> list[dict[str, object]]:
        """Return current queue state for UI rendering, including persisted deadlines."""
        return self.database.list_order_deletions()

    def recover_pending_deletions_on_startup(self) -> int:
        """Reset every persisted request to a fresh five-minute window before processing."""
        now = self._utc(self._now())
        runtime_deadline = self._monotonic() + self._delay_seconds
        with self._lock:
            count = self.database.recover_order_deletion_deadlines(
                now + timedelta(seconds=self._delay_seconds),
            )
            self._runtime_deadlines = {
                str(row["order_id"]): runtime_deadline
                for row in self.database.list_order_deletions()
            }
        if count:
            self.database.log("system", "order_deletion_startup_recovery", "ok", f"count={count}")
        return count

    def process_due_deletions(self) -> int:
        """Attempt due requests only after both persistent and monotonic deadlines pass."""
        now = self._utc(self._now())
        tick = self._monotonic()
        removed = 0
        with self._lock:
            for row in self.database.list_order_deletions():
                order_id = str(row["order_id"])
                runtime_deadline = self._runtime_deadlines.get(order_id)
                if runtime_deadline is None:
                    # Unknown runtime history is treated conservatively as a fresh window.
                    self._runtime_deadlines[order_id] = tick + self._delay_seconds
                    continue
                persisted_deadline = self._parse_utc(str(row["delete_after"]))
                if persisted_deadline is None or persisted_deadline > now or tick < runtime_deadline:
                    continue
                try:
                    outcome, reason = self.database.hard_delete_archived_order(order_id, now)
                except Exception as exc:
                    reason = f"Ошибка операции SQLite: {type(exc).__name__}"
                    self._record_failure(
                        order_id, reason, now, str(row.get("last_error") or ""),
                        action="order_deletion_failed", status="failed",
                    )
                    continue
                if outcome == "deleted":
                    self._runtime_deadlines.pop(order_id, None)
                    self.database.log("system", "order_hard_deleted", "ok", order_id)
                    removed += 1
                elif outcome == "blocked":
                    self._record_failure(order_id, reason, now, str(row.get("last_error") or ""))
                elif outcome == "not_found":
                    self._runtime_deadlines.pop(order_id, None)
                # A not_due result can follow a wall-clock correction; retain the request.
        return removed

    def _record_failure(
        self, order_id: str, reason: str, now: datetime, previous: str,
        *, action: str = "order_deletion_blocked", status: str = "warning",
    ) -> None:
        self.database.record_order_deletion_attempt(order_id, reason, now)
        if reason != previous:
            self.database.log("system", action, status, f"{order_id}: {reason}")

    def start_worker(self) -> None:
        """Start one low-frequency worker after startup recovery has run."""
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker_stop.clear()
            self._worker = Thread(target=self._worker_main, name="order-deletion-worker", daemon=True)
            self._worker.start()

    def stop_worker(self) -> None:
        """Stop the single deletion worker before the SQLite connection closes."""
        worker = self._worker
        if worker is None:
            return
        self._worker_stop.set()
        worker.join()
        self._worker = None

    def _worker_main(self) -> None:
        while not self._worker_stop.wait(DELETION_CHECK_INTERVAL_SECONDS):
            try:
                self.process_due_deletions()
            except Exception as exc:
                self.database.log("system", "order_deletion_worker", "failed", type(exc).__name__)

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @classmethod
    def _parse_utc(cls, value: str) -> datetime | None:
        try:
            return cls._utc(datetime.fromisoformat(value))
        except (TypeError, ValueError):
            return None
