"""Single-session Telethon lifecycle and safe desktop authentication flow."""

import asyncio
from dataclasses import asdict, dataclass
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import inspect
import json
from pathlib import Path
import tempfile
from threading import Event, Thread
import time
from uuid import uuid4

from freelance_bot.config import (
    DELIVERY_BARRIER_MAX_SECONDS, HISTORY_QUEUE_MAXSIZE, HISTORY_WORKERS,
    MANUAL_HISTORY_COOLDOWN_SECONDS, PROCESSING_REALTIME_BURST,
    PROCESSING_WORKERS, REALTIME_ENQUEUE_TIMEOUT_SECONDS,
    REALTIME_OVERFLOW_MAX_MESSAGES, REALTIME_QUEUE_MAXSIZE,
    REALTIME_QUEUE_PAUSE_HISTORY_PERCENT, REALTIME_QUEUE_RESUME_HISTORY_PERCENT,
    RECOVERY_MAX_WINDOW_MINUTES, RECOVERY_OVERLAP_MINUTES,
    STARTUP_HISTORY_LIMIT_PER_SOURCE, STARTUP_HISTORY_WINDOW_MINUTES,
    TELEGRAM_USER_SYNC_CONCURRENCY,
)
from freelance_bot.core.models import Account, AccountKind, TelegramMessageEnvelope, TelegramSource
from freelance_bot.security.secrets import SecretStore
from freelance_bot.telegram_user.adapter import SourceAccessError, TelegramUserAdapter


@dataclass(slots=True)
class _QueuedEnvelope:
    """Small transport-neutral item retained by the asynchronous collector only."""

    source: TelegramSource
    envelope: TelegramMessageEnvelope
    origin: str
    received_monotonic: float


class TelegramUserService:
    """Own one async User API session without sharing Bot API credentials or updates."""

    def __init__(self, database, accounts, sources, save_source, receive_order, secret_store=None,
                 deliver_order=None, flush_deliveries=None) -> None:
        self.database = database
        self.accounts = accounts
        self.sources = sources
        self.save_source = save_source
        self.receive_order = receive_order
        self.deliver_order = deliver_order
        self.flush_deliveries = flush_deliveries
        self.secret_store = secret_store or SecretStore()
        self._loop = asyncio.new_event_loop()
        self._ready = Event()
        self._thread = Thread(target=self._run_loop, name="telegram-user-api", daemon=True)
        self._thread.start()
        self._ready.wait(2)
        self._client = None
        self._adapter = None
        self._account_id = ""
        self._pending: dict[str, dict] = {}
        self._syncing_sources: set[str] = set()
        self._network_slots = asyncio.Semaphore(TELEGRAM_USER_SYNC_CONCURRENCY)
        self._realtime_queue: asyncio.Queue[_QueuedEnvelope] | None = None
        self._history_queue: asyncio.Queue[_QueuedEnvelope] | None = None
        self._processing_ready: asyncio.Event | None = None
        self._history_source_queue: asyncio.Queue[TelegramSource | None] | None = None
        self._processing_tasks: list[asyncio.Task] = []
        self._history_tasks: set[asyncio.Task] = set()
        self._history_pending = 0
        self._history_drained: asyncio.Event | None = None
        self._history_paused = False
        self._startup_barrier = False
        self._recovery_barrier = False
        self._manual_barrier = False
        self._history_barrier_started = 0.0
        self._barrier_timeout_logged = False
        self._barrier_release_task: asyncio.Task | None = None
        self._active_history_task: asyncio.Task | None = None
        self._active_history_kind = ""
        self._active_history_source_ids: set[str] = set()
        self._history_collection_ready = False
        self._pending_history_request: tuple[list[TelegramSource], str, datetime | None, datetime | None] | None = None
        self._last_manual_history_completed = 0.0
        self._watchdog_task: asyncio.Task | None = None
        self._last_disconnect_at: datetime | None = None
        self._last_reconnect_at: datetime | None = None
        self._overflow_lock: asyncio.Lock | None = None
        self._overflow_path = self._temporary_overflow_path()
        self._metrics = self._new_metrics()
        self.running = False
        self.last_error_code = ""

    @property
    def configured(self) -> bool:
        """Return whether an encrypted local User API session is available."""
        return bool(self._user_account())

    @property
    def connected(self) -> bool:
        """Return live connection state without disclosing user identity."""
        return bool(self.running and self._client and self._client.is_connected())

    @property
    def delivery_barrier_active(self) -> bool:
        """Hold new cards briefly while a finite history batch establishes order."""
        active = self._startup_barrier or self._recovery_barrier or self._manual_barrier
        if not active:
            return False
        if time.monotonic() - self._history_barrier_started < DELIVERY_BARRIER_MAX_SECONDS:
            return True
        if not self._barrier_timeout_logged:
            self._barrier_timeout_logged = True
            self.database.log("telegram_user", "delivery_barrier_timeout", "warning", self._active_history_kind)
        return False

    @property
    def history_collection_ready(self) -> bool:
        """Return true after finite History messages drained and before its fill completes."""
        return self._history_collection_ready

    @property
    def metrics(self) -> dict[str, object]:
        """Return a copy of lightweight runtime collection metrics."""
        snapshot = dict(self._metrics)
        snapshot["realtime_queue_current"] = self._realtime_queue.qsize() if self._realtime_queue else 0
        snapshot["history_queue_current"] = self._history_queue.qsize() if self._history_queue else 0
        return snapshot

    def _temporary_overflow_path(self) -> Path:
        """Keep an emergency spool outside Orders and valid after an abrupt exit."""
        path = str(getattr(self.database, "path", ""))
        if not path or path == ":memory:":
            return Path(tempfile.gettempdir()) / f"freelance-bot-user-overflow-{uuid4().hex}.jsonl"
        return Path(path).with_suffix(Path(path).suffix + ".telegram-realtime-overflow.jsonl")

    @staticmethod
    def _new_metrics() -> dict[str, object]:
        return {
            "history_workers": HISTORY_WORKERS, "sources_total": 0, "sources_processed": 0,
            "sources_failed": 0, "history_messages_loaded": 0, "realtime_messages_received": 0,
            "history_orders_created": 0,
            "realtime_queue_peak": 0, "history_queue_peak": 0, "overflow_messages_written": 0,
            "overflow_messages_processed": 0, "processing_count": 0, "processing_errors": 0,
            "flood_wait_count": 0, "flood_wait_seconds": 0, "recovery_count": 0,
            "last_recovery_duration": 0.0, "realtime_max_delay": 0.0,
        }

    def begin_login(self, api_id: int | str, api_hash: str, phone: str) -> str:
        """Request Telegram's one-time login code and return an opaque challenge ID."""
        try:
            numeric_id = int(api_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("API ID должен быть числом.") from exc
        if numeric_id <= 0 or not api_hash.strip() or not phone.strip():
            raise ValueError("Укажите API ID, API Hash и номер телефона.")
        return self._call(self._begin_login(numeric_id, api_hash.strip(), phone.strip()))

    def complete_code(self, challenge_id: str, code: str) -> str:
        """Complete code login; return `password_required` only when 2FA is enabled."""
        if not code.strip():
            raise ValueError("Введите код подтверждения.")
        return self._call(self._complete_code(challenge_id, code.strip()))

    def complete_password(self, challenge_id: str, password: str) -> None:
        """Complete 2FA login without persisting the password."""
        if not password:
            raise ValueError("Введите пароль двухфакторной авторизации.")
        self._call(self._complete_password(challenge_id, password))

    def start(self) -> None:
        """Restore the encrypted session and start listening when one is configured."""
        if self.configured:
            self._call(self._restore(), timeout=20)

    def stop(self, keep_session: bool = True) -> None:
        """Disconnect the local client; optionally retain credentials for the next launch."""
        try:
            self._call(self._stop(keep_session), timeout=15)
        except (RuntimeError, FutureTimeout):
            pass

    def forget(self) -> None:
        """Log out remotely, delete local DPAPI credentials, and retain source definitions."""
        self.stop(keep_session=False)

    def verify_source(self, reference: str) -> TelegramSource:
        """Resolve a source; persistence and activation belong to Application."""
        if not self.connected:
            raise ValueError("Сначала подключите пользовательский Telegram-аккаунт.")
        resolved = self._call(self._resolve_reference(reference), timeout=25)
        return TelegramSource(
            resolved.chat_id, resolved.name, True, "ru", 120, False,
            resolved.reference, "user", "available",
        )

    async def _resolve_reference(self, reference: str):
        """Limit User API entity verification without holding application locks."""
        async with self._network_slots:
            return await self._adapter.resolve(reference)

    def source_added(self, source: TelegramSource) -> None:
        """Schedule history/realtime for a newly persisted, verified source."""
        if self.connected and source.access_mode == "user" and source.enabled:
            self._schedule(self._start_history_pass([source], "source"))

    def source_changed(self, source: TelegramSource) -> None:
        """Schedule a verified User API source for initial sync or resume."""
        if self.connected and source.access_mode == "user" and source.enabled:
            self._schedule(self._start_history_pass([source], "source"))

    def source_removed(self, source: TelegramSource) -> None:
        """Forget a removed source from runtime without touching the session or orders."""
        self._schedule(self._stop_removed_source(source.source_id, source.chat_id))

    def request_manual_history(self) -> str:
        """Request one bounded 60-minute history pass without waiting for its scan."""
        if not self.connected:
            self.database.log("telegram_user", "manual_history", "unavailable", "user_api_disconnected")
            return "unavailable"
        try:
            future = asyncio.run_coroutine_threadsafe(self._request_manual_history(), self._loop)
            return future.result(timeout=0.5)
        except FutureTimeout:
            self.database.log("telegram_user", "manual_history", "scheduled", "coordination_timeout")
            return "scheduled"
        except Exception:
            self.database.log("telegram_user", "manual_history", "failed", "coordination_error")
            return "unavailable"

    async def _request_manual_history(self) -> str:
        """Join or queue a manual scan on the existing History pipeline."""
        if not self.running or not self._adapter:
            return "unavailable"
        selected = [source for source in self.sources().values()
                    if source.enabled and source.access_mode == "user" and source.access_status == "available"]
        if not selected:
            self.database.log("telegram_user", "manual_history", "unavailable", "no_enabled_sources")
            return "no_sources"
        now = datetime.now(timezone.utc)
        return await self._start_history_pass(
            selected, "manual", now - timedelta(minutes=STARTUP_HISTORY_WINDOW_MINUTES), now,
        )

    async def _stop_removed_source(self, source_id: str, chat_id: str) -> None:
        """Mark an in-flight sync stale; its next delivery check will stop it."""
        self._syncing_sources.discard(chat_id)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def _call(self, coroutine, timeout: float = 30):
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        try:
            return future.result(timeout=timeout)
        except FutureTimeout as exc:
            future.cancel()
            raise RuntimeError("Операция Telegram заняла слишком много времени.") from exc

    def _schedule(self, coroutine) -> None:
        """Queue a long-running source sync without blocking desktop or Bot API commands."""
        asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    @staticmethod
    def _telethon():
        try:
            from telethon import TelegramClient, events
            from telethon.errors import FloodWaitError, SessionPasswordNeededError
            from telethon.sessions import StringSession
        except ImportError as exc:
            raise RuntimeError("Для Telegram User API установите Telethon из requirements.txt.") from exc
        return TelegramClient, events, FloodWaitError, SessionPasswordNeededError, StringSession

    async def _begin_login(self, api_id: int, api_hash: str, phone: str) -> str:
        TelegramClient, _events, _FloodWaitError, _PasswordNeeded, StringSession = self._telethon()
        client = TelegramClient(StringSession(), api_id, api_hash)
        await client.connect()
        try:
            await client.send_code_request(phone)
        except Exception:
            await client.disconnect()
            raise ValueError("Не удалось запросить код. Повторите попытку позже.")
        challenge = uuid4().hex
        self._pending[challenge] = {"client": client, "api_id": api_id, "api_hash": api_hash, "phone": phone}
        return challenge

    async def _complete_code(self, challenge: str, code: str) -> str:
        state = self._pending.get(challenge)
        if state is None:
            raise ValueError("Срок действия запроса кода истёк. Запросите новый код.")
        _TelegramClient, _events, _FloodWaitError, PasswordNeeded, _StringSession = self._telethon()
        try:
            await state["client"].sign_in(phone=state["phone"], code=code)
        except PasswordNeeded:
            return "password_required"
        except Exception as exc:
            raise ValueError("Код не принят. Проверьте его или запросите новый.") from exc
        await self._finish_login(challenge)
        return "authorized"

    async def _complete_password(self, challenge: str, password: str) -> None:
        state = self._pending.get(challenge)
        if state is None:
            raise ValueError("Срок действия запроса истёк. Начните подключение заново.")
        try:
            await state["client"].sign_in(password=password)
        except Exception as exc:
            raise ValueError("Пароль не принят.") from exc
        await self._finish_login(challenge)

    async def _finish_login(self, challenge: str) -> None:
        state = self._pending.pop(challenge)
        client = state["client"]
        _TelegramClient, events, _FloodWaitError, _PasswordNeeded, StringSession = self._telethon()
        user = await client.get_me()
        account_id = f"telegram-user:{user.id}"
        secret = self.secret_store.encrypt(json.dumps({
            "api_hash": state["api_hash"], "session": StringSession.save(client.session),
        }))
        self.database.set_setting("telegram_user_api_id", str(state["api_id"]))
        account = Account(account_id, "telegram", self._masked_account_name(state["phone"], user), AccountKind.TELEGRAM_USER)
        self.accounts.add(account)
        self.database.save_secret(account_id, secret)
        self._account_id = account_id
        self._client = client
        self._adapter = TelegramUserAdapter(client, account)
        client.add_event_handler(self._on_new_message, events.NewMessage())
        self.running = True
        self.last_error_code = ""
        await self._start_pipeline()
        self._start_connection_watchdog()
        await self._sync_enabled_sources()

    async def _restore(self) -> None:
        if self.running:
            return
        account = self._user_account()
        if account is None:
            return
        try:
            api_id = int(self.database.get_setting("telegram_user_api_id"))
            secret = json.loads(self.secret_store.decrypt(self.database.load_secret(account.id)))
            TelegramClient, events, _FloodWaitError, _PasswordNeeded, StringSession = self._telethon()
            client = TelegramClient(StringSession(secret["session"]), api_id, secret["api_hash"])
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                self._requires_auth(account.id)
                return
            self._account_id = account.id
            self._client = client
            self._adapter = TelegramUserAdapter(client, account)
            client.add_event_handler(self._on_new_message, events.NewMessage())
            self.running = True
            self.last_error_code = ""
            await self._start_pipeline()
            self._start_connection_watchdog()
            await self._sync_enabled_sources()
        except Exception:
            self._requires_auth(account.id)

    async def _stop(self, keep_session: bool) -> None:
        client, account_id = self._client, self._account_id
        self.running = False
        self._client = None
        self._adapter = None
        self._account_id = ""
        if client:
            if not keep_session:
                try:
                    await client.log_out()
                except Exception:
                    pass
            await client.disconnect()
        await self._stop_pipeline()
        if not keep_session and account_id:
            self.accounts.remove(account_id)

    async def _sync_enabled_sources(self) -> None:
        sources = [source for source in self.sources().values()
                   if source.enabled and source.access_mode == "user" and source.access_status == "available"]
        await self._start_history_pass(sources, "startup")

    async def _sync_source(self, source: TelegramSource) -> None:
        """Compatibility helper retained for direct unit-level source checks.

        The live path uses the shared source queue.  Keeping this method small
        avoids binding persistent worker tasks to a test's temporary event loop.
        """
        current = next((item for item in self.sources().values() if item.source_id == source.source_id), None)
        if current is None or not current.enabled or not self._adapter:
            return
        try:
            async with self._network_slots:
                count = 0
                async for envelope in self._history_iterator(source, None):
                    if envelope.published_at and self._as_utc(envelope.published_at) < datetime.now(timezone.utc) - timedelta(minutes=STARTUP_HISTORY_WINDOW_MINUTES):
                        continue
                    self._receive_envelope(source, envelope)
                    count += 1
                    if count >= STARTUP_HISTORY_LIMIT_PER_SOURCE:
                        break
            self.database.mark_telegram_user_history_synced(source.chat_id)
        except Exception as exc:
            await self._handle_source_error(source, exc)

    async def _on_new_message(self, event) -> None:
        """Map and enqueue realtime events without waiting for filtering or SQLite."""
        if not self._adapter:
            return
        chat_id = str(getattr(event, "chat_id", ""))
        source = self.sources().get(chat_id)
        if source is None or not source.enabled or source.access_mode != "user" or source.access_status != "available":
            return
        try:
            envelope = await self._adapter.envelope_from_event(event)
            if envelope is not None:
                self._metrics["realtime_messages_received"] += 1
                await self._enqueue_realtime(source, envelope)
        except Exception as exc:
            await self._handle_source_error(source, exc)

    def _receive_envelope(self, source: TelegramSource, envelope, origin: str = "realtime") -> None:
        """Run the existing canonical pipeline, then advance a source checkpoint."""
        # Продвигаем checkpoint после обработки, чтобы ошибка не отметила сообщение как пройденное.
        result = self.receive_order(envelope)
        self.database.mark_telegram_user_message_saved(source.chat_id, envelope.message_id)
        if origin == "history" and result.is_new:
            self._metrics["history_orders_created"] += 1
        if result.is_new and result.matched and self.deliver_order:
            self.deliver_order(result.order)
        if result.is_new and result.matched:
            self.database.log(f"telegram:{source.chat_id}", "telegram_user_order", "ok", "")

    async def _start_pipeline(self) -> None:
        """Create bounded in-memory queues and the fixed processing pool once."""
        if self._realtime_queue is not None:
            return
        self._realtime_queue = asyncio.Queue(maxsize=REALTIME_QUEUE_MAXSIZE)
        self._history_queue = asyncio.Queue(maxsize=HISTORY_QUEUE_MAXSIZE)
        self._processing_ready = asyncio.Event()
        self._history_drained = asyncio.Event()
        self._history_drained.set()
        self._overflow_lock = asyncio.Lock()
        self._processing_tasks = [
            asyncio.create_task(self._processing_worker(index), name=f"telegram-processing-{index}")
            for index in range(PROCESSING_WORKERS)
        ]
        await self._recover_overflow_into_realtime()

    async def _stop_pipeline(self) -> None:
        """Stop worker tasks while retaining an emergency spool for a later launch."""
        watchdog, self._watchdog_task = self._watchdog_task, None
        if watchdog:
            watchdog.cancel()
        tasks, self._processing_tasks = self._processing_tasks, []
        all_tasks = [*tasks, *self._history_tasks]
        if self._barrier_release_task is not None:
            all_tasks.append(self._barrier_release_task)
        for task in all_tasks:
            task.cancel()
        # Unit-level callers can exercise a coroutine with asyncio.run while
        # the service owns another loop.  Production tasks all belong to this
        # loop; foreign, already-closing test loops must not be awaited here.
        current_loop = asyncio.get_running_loop()
        owned = [task for task in all_tasks if task.get_loop() is current_loop]
        if owned:
            await asyncio.gather(*owned, return_exceptions=True)
        self._history_tasks.clear()
        self._realtime_queue = None
        self._history_queue = None
        self._processing_ready = None
        self._history_source_queue = None
        self._history_drained = None
        self._overflow_lock = None
        self._barrier_release_task = None

    async def _enqueue_realtime(self, source: TelegramSource, envelope: TelegramMessageEnvelope) -> None:
        """Put a new event first; spill minimally to disk only under sustained pressure."""
        await self._start_pipeline()
        assert self._realtime_queue is not None and self._processing_ready is not None
        item = _QueuedEnvelope(source, envelope, "realtime", time.monotonic())
        try:
            await asyncio.wait_for(self._realtime_queue.put(item), REALTIME_ENQUEUE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            await self._write_overflow(item)
            return
        self._processing_ready.set()
        self._observe_queue_peaks()
        self._update_history_pressure()

    async def _enqueue_history(self, source: TelegramSource, envelope: TelegramMessageEnvelope) -> None:
        """Apply backpressure to history rather than dropping a fetched message."""
        await self._start_pipeline()
        assert self._history_queue is not None and self._processing_ready is not None
        while self._history_paused and self.running:
            await asyncio.sleep(0.05)
            self._update_history_pressure()
        self._history_pending += 1
        assert self._history_drained is not None
        self._history_drained.clear()
        try:
            await self._history_queue.put(_QueuedEnvelope(source, envelope, "history", time.monotonic()))
            self._processing_ready.set()
            self._metrics["history_messages_loaded"] += 1
            self._observe_queue_peaks()
        except Exception:
            self._history_pending -= 1
            if self._history_pending == 0:
                self._history_drained.set()
            raise

    def _observe_queue_peaks(self) -> None:
        if self._realtime_queue:
            self._metrics["realtime_queue_peak"] = max(self._metrics["realtime_queue_peak"], self._realtime_queue.qsize())
        if self._history_queue:
            self._metrics["history_queue_peak"] = max(self._metrics["history_queue_peak"], self._history_queue.qsize())

    def _update_history_pressure(self) -> None:
        if not self._realtime_queue:
            return
        size = self._realtime_queue.qsize()
        high = REALTIME_QUEUE_MAXSIZE * REALTIME_QUEUE_PAUSE_HISTORY_PERCENT / 100
        low = REALTIME_QUEUE_MAXSIZE * REALTIME_QUEUE_RESUME_HISTORY_PERCENT / 100
        if size >= high:
            self._history_paused = True
        elif self._history_paused and size <= low:
            self._history_paused = False

    async def _processing_worker(self, _index: int) -> None:
        """Prioritize fresh posts, while giving history one turn after a small burst."""
        realtime_burst = 0
        while True:
            item = await self._next_processing_item(realtime_burst)
            if item.origin == "realtime":
                realtime_burst += 1
            else:
                realtime_burst = 0
            try:
                await asyncio.to_thread(self._receive_envelope, item.source, item.envelope, item.origin)
                self._metrics["processing_count"] += 1
                if item.origin == "realtime":
                    delay = time.monotonic() - item.received_monotonic
                    self._metrics["realtime_max_delay"] = max(self._metrics["realtime_max_delay"], delay)
                    self.database.set_setting("telegram_user_last_successful_realtime_at", datetime.now(timezone.utc).isoformat())
            except Exception:
                self._metrics["processing_errors"] += 1
                self.database.record_telegram_source_error(item.source.chat_id, "user_processing_error")
            finally:
                if item.origin == "history":
                    self._history_pending = max(0, self._history_pending - 1)
                    if self._history_pending == 0 and self._history_drained:
                        self._history_drained.set()
                self._update_history_pressure()

    async def _next_processing_item(self, realtime_burst: int) -> _QueuedEnvelope:
        assert self._realtime_queue is not None and self._history_queue is not None
        assert self._processing_ready is not None
        # Один сигнал для двух очередей позволяет проверить приоритет после пробуждения.
        while self._realtime_queue.empty() and self._history_queue.empty():
            self._processing_ready.clear()
            await self._processing_ready.wait()
        critical = self._realtime_queue.qsize() >= REALTIME_QUEUE_MAXSIZE * REALTIME_QUEUE_PAUSE_HISTORY_PERCENT / 100
        if not self._realtime_queue.empty() and (critical or self._history_queue.empty() or realtime_burst < PROCESSING_REALTIME_BURST):
            return self._realtime_queue.get_nowait()
        return self._history_queue.get_nowait()

    async def _start_history_pass(self, sources: list[TelegramSource], kind: str,
                                  start_at: datetime | None = None, until: datetime | None = None) -> str:
        """Schedule or coalesce one finite pass through the shared history workers."""
        if not self.running or not self._adapter:
            return "unavailable"
        selected = [source for source in sources if source.enabled and source.access_mode == "user"
                    and source.access_status == "available"]
        if not selected:
            return "no_sources"

        active = self._active_history_task
        if active is not None and not active.done():
            active_kind = self._active_history_kind
            if kind == "manual" and active_kind in {"startup", "manual"}:
                self.database.log("telegram_user", "manual_history", "coalesced", f"active={active_kind}")
                return "joined"
            if kind == active_kind:
                new_sources = [source for source in selected
                               if (source.source_id or source.chat_id) not in self._active_history_source_ids]
                if new_sources:
                    self._queue_history_request(new_sources, kind, start_at, until)
                return "coalesced"
            if kind == "manual" and active_kind == "recovery":
                self._queue_history_request(selected, kind, start_at, until)
                self.database.log("telegram_user", "manual_history", "queued", "after_recovery")
                return "queued"
            if kind == "recovery" and active_kind in {"startup", "manual"}:
                self._queue_history_request(selected, kind, start_at, until)
                return "queued"
            if kind == "startup":
                # A newly enabled source must not be lost behind another mode.
                self._queue_history_request(selected, kind, start_at, until)
                return "queued"
            self._queue_history_request(selected, kind, start_at, until)
            return "queued"

        if kind == "manual" and self._last_manual_history_completed and (
            time.monotonic() - self._last_manual_history_completed < MANUAL_HISTORY_COOLDOWN_SECONDS
        ):
            self.database.log("telegram_user", "manual_history", "cooldown", "recent_scan_reused")
            return "cooldown"

        await self._start_pipeline()
        self._active_history_kind = kind
        self._active_history_source_ids = {source.source_id or source.chat_id for source in selected}
        self._history_barrier_started = time.monotonic()
        self._barrier_timeout_logged = False
        if kind in {"startup", "source"}:
            self._startup_barrier = True
        elif kind == "recovery":
            self._recovery_barrier = True
        elif kind == "manual":
            self._manual_barrier = True
        task = asyncio.create_task(self._run_history_pass(selected, kind, start_at, until), name=f"telegram-{kind}-history")
        self._active_history_task = task
        self._history_tasks.add(task)
        task.add_done_callback(self._history_tasks.discard)
        release_task = asyncio.create_task(
            self._release_delivery_barrier_after_deadline(task, kind),
            name="telegram-history-delivery-barrier",
        )
        self._barrier_release_task = release_task
        return "scheduled"

    def _queue_history_request(self, sources: list[TelegramSource], kind: str,
                               start_at: datetime | None, until: datetime | None) -> None:
        """Keep at most one follow-up pass and prefer a broader manual scan."""
        current = self._pending_history_request
        if current is None:
            self._pending_history_request = (list(sources), kind, start_at, until)
            return
        old_sources, old_kind, old_start, old_until = current
        by_id = {source.source_id or source.chat_id: source for source in old_sources}
        by_id.update({source.source_id or source.chat_id: source for source in sources})
        if kind == "manual" or old_kind == "manual":
            chosen_kind = "manual"
        elif kind == "recovery" or old_kind == "recovery":
            chosen_kind = "recovery"
        else:
            chosen_kind = old_kind
        starts = [value for value in (old_start, start_at) if value is not None]
        ends = [value for value in (old_until, until) if value is not None]
        self._pending_history_request = (
            list(by_id.values()), chosen_kind, min(starts) if starts else None, max(ends) if ends else None,
        )

    async def _run_history_pass(self, sources: list[TelegramSource], kind: str,
                                start_at: datetime | None, until: datetime | None) -> None:
        """Feed a shared queue of sources to finite workers, then release delivery ordering."""
        started = time.monotonic()
        message_start = int(self._metrics["history_messages_loaded"])
        failed_start = int(self._metrics["sources_failed"])
        processed_start = int(self._metrics["sources_processed"])
        created_start = int(self._metrics["history_orders_created"])
        error_code = ""
        self._metrics["sources_total"] += len(sources)
        self.database.log("telegram_user", f"{kind}_history", "started", f"sources={len(sources)} window_minutes={STARTUP_HISTORY_WINDOW_MINUTES}")
        try:
            queue: asyncio.Queue[TelegramSource | None] = asyncio.Queue()
            for source in sources:
                queue.put_nowait(source)
            workers = [asyncio.create_task(self._history_worker(queue, kind, start_at, until))
                       for _ in range(min(HISTORY_WORKERS, len(sources)))]
            await queue.join()
            for _ in workers:
                queue.put_nowait(None)
            await asyncio.gather(*workers, return_exceptions=True)
            if self._history_drained:
                await self._history_drained.wait()
            self._history_collection_ready = True
            if self.flush_deliveries:
                # This reads saved Orders through the common delivery coordinator.
                await asyncio.to_thread(self.flush_deliveries, kind)
        except asyncio.CancelledError:
            error_code = "cancelled"
            raise
        except Exception:
            error_code = "history_pass_failed"
            self.database.log("telegram_user", f"{kind}_history", "failed", error_code)
        finally:
            release_task, self._barrier_release_task = self._barrier_release_task, None
            if release_task is not None and release_task is not asyncio.current_task() and not release_task.done():
                release_task.cancel()
                await asyncio.gather(release_task, return_exceptions=True)
            if kind == "recovery":
                self._metrics["recovery_count"] += 1
                self._metrics["last_recovery_duration"] = round(time.monotonic() - started, 3)
            if kind == "manual":
                if not error_code and int(self._metrics["sources_failed"]) == failed_start:
                    self._last_manual_history_completed = time.monotonic()
                else:
                    self._last_manual_history_completed = 0.0
            self._startup_barrier = False
            self._recovery_barrier = False
            self._manual_barrier = False
            self._active_history_task = None
            self._active_history_kind = ""
            self._active_history_source_ids.clear()
            self._history_collection_ready = False
            self._history_barrier_started = 0.0
            summary = json.dumps({
                "kind": kind, "sources": len(sources),
                "processed": int(self._metrics["sources_processed"]) - processed_start,
                "failed": int(self._metrics["sources_failed"]) - failed_start,
                "messages": int(self._metrics["history_messages_loaded"]) - message_start,
                "new_orders": int(self._metrics["history_orders_created"]) - created_start,
                "duration_seconds": round(time.monotonic() - started, 3),
                "realtime_peak": self._metrics["realtime_queue_peak"],
                "history_peak": self._metrics["history_queue_peak"],
                "limiter_active": self._network_slots._value < TELEGRAM_USER_SYNC_CONCURRENCY,
                "error": error_code,
            }, ensure_ascii=False)
            self.database.log("telegram_user", f"{kind}_history_summary", "failed" if error_code else "ok", summary)
            pending, self._pending_history_request = self._pending_history_request, None
            if pending and self.running and self._adapter:
                await self._start_history_pass(*pending)

    async def _release_delivery_barrier_after_deadline(self, pass_task: asyncio.Task, kind: str) -> None:
        """Resume recent delivery after 30 seconds while a slow History pass continues."""
        await asyncio.sleep(DELIVERY_BARRIER_MAX_SECONDS)
        if self._active_history_task is not pass_task:
            return
        # The property records one timeout diagnostic and releases realtime delivery.
        _ = self.delivery_barrier_active
        self.database.log("telegram_user", "delivery_barrier", "released", f"kind={kind} limit_seconds={DELIVERY_BARRIER_MAX_SECONDS}")
        if self.flush_deliveries:
            try:
                await asyncio.to_thread(self.flush_deliveries, "barrier_timeout")
            except Exception:
                self.database.log("telegram_user", "delivery_barrier", "failed", "timeout_fill")

    async def _history_worker(self, queue: asyncio.Queue, kind: str, start_at: datetime | None,
                              until: datetime | None) -> None:
        while True:
            source = await queue.get()
            try:
                if source is None:
                    return
                await self._collect_history_source(source, kind, start_at, until)
            finally:
                queue.task_done()

    async def _collect_history_source(self, source: TelegramSource, kind: str,
                                      start_at: datetime | None, until: datetime | None) -> None:
        """Collect at most 100 recent text messages from one source with isolated retries."""
        current = next((item for item in self.sources().values() if item.source_id == source.source_id), None)
        if current is None or not current.enabled or current.access_status != "available":
            return
        if source.chat_id in self._syncing_sources:
            return
        self._syncing_sources.add(source.chat_id)
        try:
            attempts = 0
            while attempts < 3:
                try:
                    async with self._network_slots:
                        history = self._history_iterator(source, until)
                        count = 0
                        window_end = until or datetime.now(timezone.utc)
                        window_start = start_at or (window_end - timedelta(minutes=STARTUP_HISTORY_WINDOW_MINUTES))
                        async for envelope in history:
                            current = next((item for item in self.sources().values() if item.source_id == source.source_id), None)
                            if current is None or not current.enabled:
                                return
                            published = envelope.published_at
                            if published:
                                moment = self._as_utc(published)
                                if moment < self._as_utc(window_start) or moment > self._as_utc(window_end):
                                    continue
                            await self._enqueue_history(source, envelope)
                            count += 1
                            if count >= STARTUP_HISTORY_LIMIT_PER_SOURCE:
                                break
                    self._metrics["sources_processed"] += 1
                    self.database.mark_telegram_user_history_synced(source.chat_id)
                    return
                except SourceAccessError as exc:
                    self._metrics["sources_failed"] += 1
                    self.save_source(replace(source, enabled=False, access_status=exc.code))
                    return
                except Exception as exc:
                    attempts += 1
                    if self._is_flood_wait(exc):
                        seconds = int(getattr(exc, "seconds", 0) or 0)
                        self._metrics["flood_wait_count"] += 1
                        self._metrics["flood_wait_seconds"] += seconds
                        retry = datetime.now(timezone.utc) + timedelta(seconds=seconds)
                        self.database.record_telegram_user_wait(source.chat_id, retry)
                        await asyncio.sleep(max(0, seconds))
                    elif attempts < 3:
                        await asyncio.sleep(0.25 * attempts)
                    if attempts >= 3:
                        self._metrics["sources_failed"] += 1
                        self.database.record_telegram_source_error(source.chat_id, "user_history_error")
                        self.save_source(replace(source, access_status="error"))
                        return
        finally:
            self._syncing_sources.discard(source.chat_id)

    def _history_iterator(self, source: TelegramSource, until: datetime | None):
        """Support narrow fake adapters while requesting the real adapter's limit."""
        assert self._adapter is not None
        kwargs = {"hours": STARTUP_HISTORY_WINDOW_MINUTES // 60, "until": until}
        try:
            parameters = inspect.signature(self._adapter.history).parameters
            if "limit" in parameters:
                kwargs["limit"] = STARTUP_HISTORY_LIMIT_PER_SOURCE
        except (TypeError, ValueError):
            kwargs["limit"] = STARTUP_HISTORY_LIMIT_PER_SOURCE
        return self._adapter.history(source, **kwargs)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

    @staticmethod
    def _is_flood_wait(exc: Exception) -> bool:
        return hasattr(exc, "seconds") and type(exc).__name__.lower().endswith("floodwaiterror")

    async def _write_overflow(self, item: _QueuedEnvelope) -> None:
        """Append the minimal envelope to a bounded emergency spool, never Orders SQLite."""
        assert self._overflow_lock is not None
        async with self._overflow_lock:
            entries = await asyncio.to_thread(self._load_overflow_entries)
            if len(entries) >= REALTIME_OVERFLOW_MAX_MESSAGES:
                self.database.log("telegram_user", "realtime_overflow", "critical", "capacity_reached")
                # Backpressure is preferable to silently losing a live post.
                assert self._realtime_queue is not None and self._processing_ready is not None
                await self._realtime_queue.put(item)
                self._processing_ready.set()
                return
            entries.append(self._serialize_overflow(item))
            await asyncio.to_thread(self._write_overflow_entries, entries)
            self._metrics["overflow_messages_written"] += 1

    def _load_overflow_entries(self) -> list[dict]:
        if not self._overflow_path.exists():
            return []
        entries = []
        for line in self._overflow_path.read_text(encoding="utf-8").splitlines():
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries

    def _write_overflow_entries(self, entries: list[dict]) -> None:
        if not entries:
            self._overflow_path.unlink(missing_ok=True)
            return
        self._overflow_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._overflow_path.with_suffix(self._overflow_path.suffix + ".tmp")
        temporary.write_text("\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries) + "\n", encoding="utf-8")
        temporary.replace(self._overflow_path)

    @staticmethod
    def _serialize_overflow(item: _QueuedEnvelope) -> dict:
        envelope = asdict(item.envelope)
        for key in ("published_at", "received_at"):
            value = envelope.get(key)
            if isinstance(value, datetime):
                envelope[key] = value.isoformat()
        return {"source": asdict(item.source), "envelope": envelope}

    @staticmethod
    def _deserialize_overflow(entry: dict) -> _QueuedEnvelope | None:
        try:
            source = TelegramSource(**entry["source"])
            data = dict(entry["envelope"])
            for key in ("published_at", "received_at"):
                if data.get(key):
                    data[key] = datetime.fromisoformat(data[key])
            return _QueuedEnvelope(source, TelegramMessageEnvelope(**data), "realtime", time.monotonic())
        except (KeyError, TypeError, ValueError):
            return None

    async def _recover_overflow_into_realtime(self) -> None:
        """Consume a previous temporary spool before history gets a chance to run."""
        assert self._overflow_lock is not None and self._realtime_queue is not None
        assert self._processing_ready is not None
        async with self._overflow_lock:
            entries = await asyncio.to_thread(self._load_overflow_entries)
            if not entries:
                return
            remaining: list[dict] = []
            for entry in entries:
                item = self._deserialize_overflow(entry)
                if item is None:
                    continue
                try:
                    self._realtime_queue.put_nowait(item)
                    self._processing_ready.set()
                    self._metrics["overflow_messages_processed"] += 1
                except asyncio.QueueFull:
                    remaining.append(entry)
            await asyncio.to_thread(self._write_overflow_entries, remaining)
            self._observe_queue_peaks()

    def _start_connection_watchdog(self) -> None:
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._connection_watchdog(), name="telegram-user-connection-watchdog")

    async def _connection_watchdog(self) -> None:
        """Recover listening first, then inspect the short downtime gap once."""
        was_connected = True
        while self.running and self._client:
            await asyncio.sleep(5)
            connected = bool(self._client and self._client.is_connected())
            if not connected and was_connected:
                self._last_disconnect_at = datetime.now(timezone.utc)
                self.database.set_setting("telegram_user_last_disconnect_at", self._last_disconnect_at.isoformat())
            if not connected:
                try:
                    await self._client.connect()
                    connected = bool(self._client.is_connected())
                except Exception:
                    connected = False
            if connected and not was_connected:
                await self._run_recovery_history()
            was_connected = connected

    async def _run_recovery_history(self) -> None:
        """Queue only the bounded interval missed while the account was disconnected."""
        reconnect = datetime.now(timezone.utc)
        disconnect = self._last_disconnect_at or reconnect
        start = max(reconnect - timedelta(minutes=RECOVERY_MAX_WINDOW_MINUTES),
                    disconnect - timedelta(minutes=RECOVERY_OVERLAP_MINUTES))
        self._last_reconnect_at = reconnect
        self.database.set_setting("telegram_user_last_reconnect_at", reconnect.isoformat())
        sources = [source for source in self.sources().values()
                   if source.enabled and source.access_mode == "user" and source.access_status == "available"]
        await self._start_history_pass(sources, "recovery", start, reconnect)

    async def _handle_source_error(self, source: TelegramSource, exc: Exception) -> None:
        # Adapter-level access errors are dependency-independent; do not import
        # Telethon merely to persist a source that was explicitly denied.
        if isinstance(exc, SourceAccessError):
            self.save_source(replace(source, enabled=False, access_status=exc.code))
            return
        _TelegramClient, _events, FloodWaitError, _PasswordNeeded, _StringSession = self._telethon()
        if isinstance(exc, FloodWaitError):
            retry = datetime.now(timezone.utc) + timedelta(seconds=int(exc.seconds))
            self.database.record_telegram_user_wait(source.chat_id, retry)
            self.save_source(replace(source, access_status="flood_wait"))
            self.last_error_code = "flood_wait"
            asyncio.create_task(self._resume_source_after(source, retry, "available"))
            return
        self.database.record_telegram_source_error(source.chat_id, "user_api_error")
        self.save_source(replace(source, access_status="error"))
        self.last_error_code = "user_api_error"
        asyncio.create_task(self._resume_source_after(source, datetime.now(timezone.utc) + timedelta(seconds=5), "available"))

    async def _resume_source_after(self, source: TelegramSource, retry_at: datetime, status: str) -> None:
        """Retry one source after a bounded wait without affecting other Telegram sources."""
        delay = max(0, (retry_at - datetime.now(timezone.utc)).total_seconds())
        await asyncio.sleep(delay)
        current = self.sources().get(source.chat_id)
        if self.running and current and current.enabled and current.access_status in {"flood_wait", "error"}:
            resumed = replace(current, access_status=status)
            self.save_source(resumed)
            await self._sync_source(resumed)

    def _user_account(self) -> Account | None:
        return next((item for item in self.accounts.all() if item.kind == AccountKind.TELEGRAM_USER), None)

    def _requires_auth(self, account_id: str) -> None:
        account = self.accounts.get(account_id)
        if account:
            self.accounts.pause_for_attention(account_id)
        self.running = False
        self.last_error_code = "requires_auth"

    @staticmethod
    def _masked_account_name(phone: str, user) -> str:
        digits = "".join(char for char in phone if char.isdigit())
        masked = f"+***{digits[-4:]}" if len(digits) >= 4 else "Telegram user"
        username = str(getattr(user, "username", "") or "")
        return f"{masked} (@{username})" if username else masked

    @staticmethod
    def _parse_time(value: str) -> datetime | None:
        try:
            return datetime.fromisoformat(value) if value else None
        except ValueError:
            return None
