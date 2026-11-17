"""Application composition root and safe startup/shutdown lifecycle."""

import os
import re
from concurrent.futures import ThreadPoolExecutor
from threading import RLock
import time
from dataclasses import replace
from urllib.parse import urlparse
from freelance_bot.config import load_local_env
from freelance_bot.config import DELIVERY_BARRIER_MAX_SECONDS, MANUAL_HISTORY_COOLDOWN_SECONDS
from freelance_bot.accounts.manager import AccountManager
from freelance_bot.browser_automation.manager import BrowserSessionManager, MemoryGuard
from freelance_bot.scheduler.queue import DispatchQueue
from freelance_bot.storage.database import Database
from freelance_bot.core.deletion_service import DeletionService
from freelance_bot.telegram_bot.service import TelegramService
from freelance_bot.telegram_bot.transport import TelegramCallbackResult, TelegramPolling, TelegramReply
from freelance_bot.telegram_bot.feed import TelegramOrderFeed
from freelance_bot.telegram_user.service import TelegramUserService
from freelance_bot.storage.profile import ProfileStore
from freelance_bot.core.order_service import OrderService
from freelance_bot.core.filtering import matches
from freelance_bot.core.models import OrderClassification, OrderStatus
from freelance_bot.core.source_import import ImportResult, SourceImportService
from freelance_bot.core.models import ProcessState, TelegramSource
from datetime import datetime, timedelta, timezone
from freelance_bot.core.models import DispatchJob, JobStatus, ProposalMode, ProposalProfile
from freelance_bot.ai.proposals import ProposalGenerator
from uuid import uuid4


class Application:
    def __init__(self) -> None:
        load_local_env()
        self.database = Database(os.getenv("DATABASE_PATH", "data/freelance_bot.sqlite3"))
        self.archive_expired_orders()
        self.deletion_service = DeletionService(self.database)
        self.deletion_service.recover_pending_deletions_on_startup()
        self.accounts = AccountManager(int(os.getenv("MAX_ACTIVE_ACCOUNTS", "50")), self.database.load_accounts(), self.database)
        self.browser = BrowserSessionManager(
            int(os.getenv("MAX_BROWSER_WORKERS", "1")),
            MemoryGuard(int(os.getenv("MEMORY_SOFT_LIMIT_MB", "4096")), int(os.getenv("MEMORY_HARD_LIMIT_MB", "5120"))),
        )
        self.queue = DispatchQueue(self.database)
        self.queue.restore(self.database.load_jobs())
        saved_sources = self.database.load_telegram_sources()
        self.telegram_sources: dict[str, TelegramSource] = {source.chat_id: source for source in saved_sources}
        self._source_lock = RLock()
        for chat_id in {x.strip() for x in os.getenv("TELEGRAM_SOURCE_CHAT_IDS", "").split(",") if x.strip()}:
            self.telegram_sources.setdefault(chat_id, TelegramSource(chat_id))
        self.watched_chat_ids = {source.chat_id for source in self.telegram_sources.values() if source.enabled}
        self.parsing_state = ProcessState.OFF
        self.automation_state = ProcessState.OFF
        self.posting_state = ProcessState.OFF
        saved_bot_enabled = self.database.get_setting("bot_enabled", "")
        if not saved_bot_enabled:
            saved_bot_enabled = self.database.get_setting("telegram_notifications_enabled", "1")
            self.database.set_setting("bot_enabled", saved_bot_enabled)
        self.notifications_enabled = saved_bot_enabled == "1"
        self.orders = OrderService(self.database, self.database.load_search_profile())
        self.orders.recover_telegram_metadata()
        self.proposals = ProposalGenerator()
        self.profile_store = ProfileStore()
        self.proposal_profile = self.profile_store.load()
        allowed = {int(x) for x in os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").split(",") if x.strip().isdigit()}
        admin_value = os.getenv("TELEGRAM_ADMIN_USER_IDS", "").strip()
        configured_admins = {int(x) for x in admin_value.split(",") if x.strip().isdigit()}
        self.admin_user_ids = (configured_admins & allowed) if admin_value else set(allowed)
        self.telegram = TelegramService(os.getenv("TELEGRAM_BOT_TOKEN", ""), allowed, self.admin_user_ids)
        self.telegram_transport = TelegramPolling(
            os.getenv("TELEGRAM_BOT_TOKEN", ""), allowed,
            checkpoint_load=lambda: self.database.get_setting("telegram_updates_offset", "0"),
            checkpoint_save=lambda offset: self.database.set_setting("telegram_updates_offset", str(offset)),
        )
        self.telegram_user = TelegramUserService(
            self.database, self.accounts, self.telegram_source_snapshot,
            self.add_telegram_source,
            lambda envelope: self._on_telegram_user_message(envelope, deliver=False),
            deliver_order=self._queue_user_api_delivery,
            flush_deliveries=self._flush_user_api_delivery_batch,
        )
        self.source_importer = SourceImportService(self)
        self._bot_import_executor: ThreadPoolExecutor | None = None
        self._telegram_delivery_executor: ThreadPoolExecutor | None = None
        self._telegram_fill_executor: ThreadPoolExecutor | None = None
        self._telegram_delivery_lock = RLock()
        self._telegram_delivery_queued: set[tuple[str, str]] = set()
        self._deferred_delivery_orders: dict[str, object] = {}
        self._telegram_fill_lock = RLock()
        self._telegram_fill_future = None
        self._bot_enabled_lock = RLock()
        self._last_start_fill_requested = 0.0
        self._register_telegram_handlers()

    def archive_expired_orders(self, now: datetime | None = None) -> int:
        """Run the fixed 12-hour archive check and log only actual transitions."""
        archived = self.database.archive_expired_orders(now)
        if archived:
            self.database.log("system", "orders_auto_archived", "ok", str(archived))
        return archived

    def _register_telegram_handlers(self) -> None:
        """Register the stable public commands, owner buttons, intake, and callbacks."""
        self.telegram_transport.command("start", self._start_command)
        self.telegram_transport.command("stop", self._stop_command)
        self.telegram_transport.command("menu", self._menu_command)
        self.telegram_transport.command("status", self._status_command)
        self.telegram_feed = TelegramOrderFeed(self.database, self.is_admin)
        self.telegram_transport.command("orders", self._orders_command)
        self.telegram_transport.menu("📋 Заказы", self._orders_command)
        self.telegram_transport.menu("📊 Статус", self._status_command)
        self.telegram_transport.menu("▶️ Запустить", self._start_command)
        self.telegram_transport.menu("⏹ Остановить", self._stop_command)
        self.telegram_transport.on_callback(self._telegram_callback)
        self.telegram_transport.on_post(self._on_telegram_post)
        self.telegram_transport.on_document(self._on_source_document)
        self.telegram_transport.on_idle(self.process_pending_telegram_sources)
        self.telegram_transport.on_self_check_error(
            lambda reason: self.database.log("telegram", "bot_api_self_check", "failed", reason)
        )

    def run(self) -> None:
        """Start the Telegram control plane and the Windows dashboard."""
        try:
            from freelance_bot.ui.window import Dashboard
        except ImportError as exc:
            self.database.close()
            raise RuntimeError("Для окна приложения установите зависимости: python -m pip install -r requirements.txt") from exc
        self.deletion_service.start_worker()
        if self.database.get_setting('parsing_start_on_launch', '1') == '1':
            self.start_services()
        else:
            self.parsing_state = ProcessState.PAUSED
        Dashboard(self).run()

    def shutdown(self) -> None:
        self.deletion_service.stop_worker()
        self.stop_services()
        self.database.close()

    def start_services(self) -> None:
        """Start lightweight local services without pretending they are connected."""
        self.telegram.start()
        self.telegram_transport.start()
        try:
            self.telegram_user.start()
        except Exception:
            # User API availability is independent of the existing Bot API control plane.
            self.database.log("telegram_user", "telegram_user_start", "failed", "")
        if self.notifications_enabled and not getattr(self.telegram_user, "delivery_barrier_active", False):
            # Restore persisted delivery state even when User API is unavailable;
            # a running Startup/Recovery pass will perform its own ordered fill.
            self._request_delivery_fill("desktop_startup")
        self.parsing_state = ProcessState.RUNNING if (self.telegram.running or self.telegram_user.running) else ProcessState.OFF
        self.database.log("system", "application_start", "ok" if self.telegram.running else "needs_setup", self.telegram.last_error)

    def stop_services(self) -> None:
        self.telegram_user.stop()
        self.telegram.stop()
        self.telegram_transport.stop()
        self.parsing_state = ProcessState.OFF
        self.automation_state = ProcessState.OFF
        self.posting_state = ProcessState.OFF
        bot_import_executor = getattr(self, "_bot_import_executor", None)
        if bot_import_executor is not None:
            # Let the current row finish before Database.close(); queued imports
            # are cancelled, while already persisted sources remain consistent.
            bot_import_executor.shutdown(wait=True, cancel_futures=True)
            self._bot_import_executor = None
        fill_executor = getattr(self, "_telegram_fill_executor", None)
        if fill_executor is not None:
            fill_executor.shutdown(wait=True, cancel_futures=True)
            self._telegram_fill_executor = None
        delivery_executor = getattr(self, "_telegram_delivery_executor", None)
        if delivery_executor is not None:
            delivery_executor.shutdown(wait=True, cancel_futures=True)
            self._telegram_delivery_executor = None
        self.database.log("system", "application_stop", "ok")

    def _on_source_document(self, user_id: int, document: dict, chat: dict):
        """Accept an administrator TXT document and run the shared importer once."""
        if not self.is_admin(user_id):
            return "Только администратор может импортировать источники."
        file_name = str(document.get("file_name") or "").strip()
        file_id = str(document.get("file_id") or "").strip()
        if not file_id or (file_name and not file_name.casefold().endswith(".txt")):
            return "Отправьте TXT-файл со списком Telegram-источников."
        try:
            content = self.telegram_transport.download_document(file_id)
        except Exception:
            return "Не удалось скачать TXT-файл из Telegram."
        if self._bot_import_executor is None:
            self._bot_import_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="telegram-import")
        future = self._bot_import_executor.submit(self.source_importer.import_bytes, content)
        chat_id = chat.get("id")

        def complete(done):
            try:
                result = done.result()
                self.telegram_transport.send_message(chat_id, self._format_import_result(result))
            except Exception:
                self.telegram_transport.send_message(chat_id, "Импорт TXT не выполнен.")

        future.add_done_callback(complete)
        return []

    @staticmethod
    def _format_import_result(result: ImportResult) -> str:
        state = "Импорт отменён" if result.cancelled else "Импорт завершён"
        return (
            f"{state}\nВсего: {result.total}\nОбработано: {result.processed}\n"
            f"Добавлено: {result.added}\nУже было: {result.duplicate}\n"
            f"Нет доступа: {result.no_access}\nНекорректных: {result.invalid}\nОшибок: {result.errors}"
        )

    def status_text(self) -> str:
        metrics = self.database.telegram_runtime_metrics()
        bot_api = getattr(self, "telegram_transport", None)
        user_api = getattr(self, "telegram_user", None)
        user_configured = bool(user_api and user_api.configured)
        user_connected = bool(user_api and user_api.connected)
        bot_connected = bool(bot_api and bot_api.connected)
        bot_configured = bool(bot_api and bot_api.enabled)
        lines = [
            "📊 Freelance Bot",
            f"Бот: {'🟢 запущен' if self.notifications_enabled else '⏸ остановлен'}",
            f"Bot API: {'🟢' if bot_connected else ('🔴' if bot_configured else '🟡 не настроен')}",
            f"User API: {'🟢' if user_connected else ('🟡 не настроен' if not user_configured else '🔴')}",
            f"Подходящих: {metrics['matched']}",
            f"Не определено: {metrics['undetermined']}",
            f"Архив: {metrics['archived']}",
            f"Ожидают удаления: {metrics['pending_deletions']}",
            f"Источники: {metrics['source_enabled']} / {metrics['source_total']}",
            f"Ошибки источников: {metrics['source_errors']}",
        ]
        last_post = self._age_label(metrics["last_post"])
        last_matched = self._age_label(metrics["last_matched_order"])
        if last_post:
            lines.append(f"Последний пост: {last_post}")
        if last_matched:
            lines.append(f"Последний подходящий заказ: {last_matched}")
        return "\n".join(lines)

    @staticmethod
    def _age_label(value: str) -> str:
        """Format a stored timestamp as elapsed time without inventing missing metrics."""
        if not value:
            return ""
        try:
            moment = datetime.fromisoformat(value)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            seconds = max(0, int((datetime.now(timezone.utc) - moment.astimezone(timezone.utc)).total_seconds()))
        except (TypeError, ValueError):
            return ""
        if seconds < 60:
            return f"{seconds} сек. назад"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes} мин. назад"
        hours = minutes // 60
        if hours < 24:
            return f"{hours} ч. назад"
        return f"{hours // 24} дн. назад"

    def _start_command(self, user_id: int, _args: str):
        """Enable delivery and request one asynchronous fill without restarting collection."""
        with getattr(self, "_bot_enabled_lock", RLock()):
            was_enabled = self.notifications_enabled
            self.notifications_enabled = True
            self.database.set_setting("bot_enabled", "1")
            self.database.set_setting("telegram_notifications_enabled", "1")
        self.database.log("telegram", "delivery_start_command", "ok", f"enabled_before={int(was_enabled)} enabled_after=1")
        user_api = getattr(self, "telegram_user", None)
        barrier_active = bool(getattr(user_api, "delivery_barrier_active", False)) if user_api else False
        fill_lock = getattr(self, "_telegram_fill_lock", None)
        if fill_lock is None:
            fill_lock = self._telegram_fill_lock = RLock()
        with fill_lock:
            now = time.monotonic()
            should_fill = not was_enabled or now - getattr(self, "_last_start_fill_requested", 0.0) >= MANUAL_HISTORY_COOLDOWN_SECONDS
            if should_fill:
                self._last_start_fill_requested = now
        if should_fill and not barrier_active:
            self._request_delivery_fill("start", user_id=user_id)
        elif should_fill and barrier_active:
            self.database.log("telegram", "delivery_fill", "deferred", "startup_or_recovery_barrier")
        state = "Получение заказов уже включено." if was_enabled else "Получение заказов в Telegram-боте включено."
        if barrier_active:
            detail = "Синхронизация Telegram продолжается; недоставленные заказы будут добавлены после неё."
        elif should_fill:
            detail = "Проверка недоставленных заказов запущена."
        else:
            detail = "Проверка недавно выполнялась; новые заказы продолжают приходить автоматически."
        return TelegramReply(f"{state}\n{detail}", self._main_menu())

    def _stop_command(self, _user: int, _args: str) -> TelegramReply:
        """Disable owner delivery while desktop and Telegram collection continue."""
        with getattr(self, "_bot_enabled_lock", RLock()):
            self.notifications_enabled = False
            self.database.set_setting("bot_enabled", "0")
            self.database.set_setting("telegram_notifications_enabled", "0")
        return TelegramReply(
            "Получение заказов в Telegram остановлено. Сбор заказов на ПК продолжается.",
            self._main_menu(),
        )

    def _menu_command(self, _user: int, _args: str) -> TelegramReply:
        """Always create a fresh persistent control menu independent of old messages."""
        return TelegramReply("Главное меню Freelance Bot", self._main_menu())

    def _status_command(self, _user: int, _args: str) -> TelegramReply:
        """Show runtime states and the current menu."""
        return TelegramReply(self.status_text(), self._main_menu())

    def _settings_command(self, _user: int, _args: str) -> str:
        """Return a read-only runtime summary without disclosing secrets or message text."""
        sources = self.telegram_source_snapshot()
        enabled_sources = sum(source.enabled for source in sources.values())
        notifications = "включены" if self.notifications_enabled else "выключены"
        return (
            f"Мониторинг: {'работает' if self.telegram.running else 'остановлен'}\n"
            f"Уведомления: {notifications}\n"
            f"Источники: {enabled_sources}/{len(sources)} включено\n"
            f"Заказов сохранено: {self.database.count('orders')}\n"
            "Изменение источников и правил доступно в desktop-приложении."
        )

    def _orders_command(self, user_id: int, args: str):
        """Run one shared manual History recovery and then let delivery fill from SQLite."""
        user_api = getattr(self, "telegram_user", None)
        history_state = "unavailable"
        if user_api is not None and hasattr(user_api, "request_manual_history"):
            try:
                history_state = user_api.request_manual_history()
            except Exception:
                self.database.log("telegram_user", "manual_history", "failed", "request_error")
        if not self.notifications_enabled:
            if history_state in {"scheduled", "joined", "queued", "coalesced"}:
                message = "Ручная проверка Telegram запущена. Получение карточек остановлено; сбор и сохранение продолжаются."
            else:
                message = "Получение заказов остановлено. Сбор и сохранение продолжаются; ручная проверка Telegram сейчас недоступна."
            return TelegramReply(message, self._main_menu())
        if history_state in {"scheduled", "joined", "queued", "coalesced"}:
            message = "Ручная проверка Telegram запущена. Подходящие недоставленные заказы будут добавлены после обработки истории."
            return TelegramReply(message, self._main_menu())
        self.database.log("telegram_user", "manual_history", "unavailable", history_state)
        self._request_delivery_fill("orders_fallback", user_id=user_id)
        return TelegramReply(
            "Историю Telegram сейчас проверить не удалось. Запущена догрузка сохранённых недоставленных заказов.",
            self._main_menu(),
        )

    def _telegram_callback(self, user_id: int, data: str):
        """Resolve menu and legacy card callbacks safely after order deletion."""
        if data == "menu":
            return TelegramCallbackResult(messages=[TelegramReply("Главное меню Freelance Bot", self._main_menu())])
        if data.startswith("feed:"):
            parts = data.split(":")
            order_id = parts[3] if len(parts) >= 4 else ""
            if order_id and self.database.get_order(order_id) is None:
                unavailable = "Заказ уже удалён или недоступен."
                return TelegramCallbackResult(
                    edit=TelegramReply(unavailable, {"inline_keyboard": []}), notice=unavailable,
                )
        return self.telegram_feed.callback(user_id, data)

    def _main_menu(self) -> dict:
        """Return the persistent owner menu without exposing any sensitive values."""
        return {
            "keyboard": [
                [{"text": "📋 Заказы"}, {"text": "📊 Статус"}],
                [{"text": "▶️ Запустить" if not self.notifications_enabled else "⏹ Остановить"}],
            ],
            "resize_keyboard": True,
        }

    def is_admin(self, user_id: int) -> bool:
        """Use the existing owner allowlist, optionally narrowed by admin IDs."""
        return self.telegram.authorize_admin(user_id)

    def _sources_command(self, user_id: int, _args: str):
        """Show shared SQLite sources and instructions without modifying them."""
        sources = list(self.telegram_source_snapshot().values())
        if not sources:
            text = "Источники Telegram пока не добавлены."
        else:
            lines = ["Источники Telegram:"]
            for source in sources:
                state = source.access_status if source.access_mode == "user" else ("Ожидает подключения" if source.pending_connection else ("Включён" if source.enabled else "Выключен"))
                label = source.name or source.chat_id
                lines.append(f"• {label} — {state}\n  chat_id: {source.chat_id}\n  source_id: {source.source_id}")
            text = "\n".join(lines)
        if self.is_admin(user_id):
            text += (
                "\n\nДобавить: /source_add @username, https://t.me/username, "
                "https://t.me/+invite_code или числовой chat_id.\n"
                "Управление: /source_enable ID, /source_disable ID, /source_remove ID.\n"
                "User API не вступает в чаты сам: для приватного источника сначала вступите в него обычным Telegram-клиентом."
            )
        return TelegramReply(text, self._main_menu())

    def _source_add_command(self, user_id: int, args: str):
        if not self.is_admin(user_id):
            return "Только администратор может добавлять источники."
        if not args.strip():
            return "Использование: /source_add @username, https://t.me/username, ссылка-приглашение или числовой chat_id."
        source, resolved = self.add_telegram_source_reference(args)
        if resolved:
            return f"Источник сохранён: {source.name or source.chat_id}. Статус: {source.access_status}."
        return (
            f"Источник сохранён со статусом «Ожидает подключения»: {source.chat_id}.\n"
            "Подключите Telegram User API и убедитесь, что аккаунт уже имеет доступ к источнику, затем повторите добавление."
        )

    def _source_remove_command(self, user_id: int, args: str):
        if not self.is_admin(user_id):
            return "Только администратор может удалять источники."
        source_id = args.strip()
        if not source_id or self._source_by_id(source_id) is None:
            return "Укажите сохранённый source_id источника из раздела «Источники»."
        self.remove_telegram_source(source_id)
        return "Источник удалён. Уже сохранённые объявления остались в базе."

    def _set_source_enabled_command(self, user_id: int, args: str, enabled: bool):
        if not self.is_admin(user_id):
            return "Только администратор может менять источники."
        source_id = args.strip()
        source = self._source_by_id(source_id)
        if source is None:
            return "Укажите сохранённый source_id источника из раздела «Источники»."
        self.add_telegram_source(replace(source, enabled=enabled))
        return "Источник включён." if enabled else "Источник выключен."

    def _source_enable_command(self, user_id: int, args: str):
        return self._set_source_enabled_command(user_id, args, True)

    def _source_disable_command(self, user_id: int, args: str):
        return self._set_source_enabled_command(user_id, args, False)

    def add_telegram_source_reference(self, reference: str, name: str = '', enabled: bool = True,
                                      language: str = 'ru', poll_interval_seconds: int = 120) -> tuple[TelegramSource, bool]:
        """Resolve through User API when connected, otherwise retain Bot API compatibility."""
        value = reference.strip()
        if not value:
            raise ValueError('Укажите @username, ссылку Telegram или числовой chat_id.')
        resolved_reference = self._normalize_telegram_source_reference(value)
        user_api = getattr(self, "telegram_user", None)
        if user_api and user_api.connected:
            try:
                resolved = user_api.verify_source(resolved_reference)
                existing = self._source_by_chat_id(resolved.chat_id)
                if existing is not None:
                    return existing, True
                source = replace(
                    resolved, name=name.strip() or resolved.name, enabled=bool(enabled), language=language,
                    poll_interval_seconds=int(poll_interval_seconds), reference=resolved_reference,
                )
                old_pending = self.telegram_source_snapshot().get(resolved_reference)
                if old_pending and old_pending.chat_id != source.chat_id and old_pending.pending_connection:
                    self.remove_telegram_source(old_pending.source_id)
                source = self.add_telegram_source(source)
                self._initialize_new_user_source(source)
                return source, True
            except ValueError:
                source = TelegramSource(
                    resolved_reference, name.strip() or resolved_reference, False, language, int(poll_interval_seconds),
                    True, resolved_reference, "user", "forbidden",
                )
                source = self.add_telegram_source(source)
                return source, False
        if user_api and user_api.configured:
            source = TelegramSource(
                resolved_reference, name.strip() or resolved_reference, False, language, int(poll_interval_seconds),
                True, resolved_reference, "user", "requires_auth",
            )
            source = self.add_telegram_source(source)
            return source, False
        chat = None
        if resolved_reference.lstrip('-').isdigit() or resolved_reference.startswith('@'):
            try:
                chat = self.telegram_transport.get_chat(resolved_reference)
            except Exception:
                chat = None
        if chat and chat.get('id') is not None:
            chat_id = str(chat['id'])
            existing = self._source_by_chat_id(chat_id)
            if existing is not None:
                return existing, True
            source = TelegramSource(
                chat_id, name.strip() or str(chat.get('title') or chat.get('username') or resolved_reference),
                bool(enabled), language, int(poll_interval_seconds), True, resolved_reference, "bot", "available",
            )
            old_pending = self.telegram_source_snapshot().get(resolved_reference)
            if old_pending and old_pending.pending_connection:
                self.remove_telegram_source(old_pending.source_id)
            source = self.add_telegram_source(source)
            return source, True
        source = TelegramSource(
            resolved_reference, name.strip() or resolved_reference, False, language, int(poll_interval_seconds), True,
            resolved_reference, "bot", "pending",
        )
        source = self.add_telegram_source(source)
        return source, False

    @staticmethod
    def _normalize_telegram_source_reference(value: str) -> str:
        if value.lstrip('-').isdigit():
            return value
        parsed = urlparse(value)
        if parsed.scheme in {'https', 'http'} and parsed.netloc.casefold() in {'t.me', 'www.t.me', 'telegram.me', 'www.telegram.me'}:
            path = parsed.path.strip('/')
            if path.startswith('+') and len(path) > 1:
                return f"https://t.me/{path}"
            if re.fullmatch(r'[A-Za-z0-9_]{5,32}', path):
                return f"@{path}"
        if re.fullmatch(r'@[A-Za-z0-9_]{5,32}', value):
            return value
        if re.fullmatch(r'[A-Za-z0-9_]{5,32}', value):
            return f"@{value}"
        raise ValueError('Поддерживаются @username, ссылка t.me или числовой chat_id.')

    def _watch_command(self, _user: int, args: str) -> str:
        if not args:
            return "Использование: /watch ID_чата"
        chat_id = args.split()[0]
        self.add_telegram_source(TelegramSource(chat_id))
        return f"Источник добавлен: {chat_id}"

    def _unwatch_command(self, _user: int, args: str) -> str:
        self.remove_telegram_source(args.split()[0] if args else "")
        return "Источник удалён."

    def add_telegram_source(self, source: TelegramSource) -> TelegramSource:
        with self._source_guard():
            existing = self._source_by_chat_id(source.chat_id)
            if existing is not None:
                source = replace(source, source_id=existing.source_id)
            elif not source.source_id:
                source = replace(source, source_id=uuid4().hex)
            self.telegram_sources[source.chat_id] = source
            self.database.save_telegram_source(source)
            self.watched_chat_ids = {item.chat_id for item in self.telegram_sources.values() if item.enabled}
        self.database.log("system", "telegram_source_saved", source.access_status if source.pending_connection else "ok", source.chat_id)
        return source

    def _initialize_new_user_source(self, source: TelegramSource) -> None:
        """Create the first checkpoint and schedule sync for a newly verified source."""
        if source.access_mode != "user" or not source.enabled or source.access_status != "available":
            return
        self.database.begin_telegram_user_initial_sync(source.chat_id, datetime.now(timezone.utc))
        user_api = getattr(self, "telegram_user", None)
        if user_api:
            user_api.source_added(source)

    def remove_telegram_source(self, source_id: str) -> None:
        """Remove a source by stable source_id and stop its User API intake."""
        source = self._source_by_id(source_id)
        if source is None:
            return
        with self._source_guard():
            self.telegram_sources.pop(source.chat_id, None)
            self.database.delete_telegram_source(source.source_id)
            self.watched_chat_ids.discard(source.chat_id)
        user_api = getattr(self, "telegram_user", None)
        if user_api:
            user_api.source_removed(source)
        self.database.log("system", "telegram_source_removed", "ok", source.source_id)

    def _source_by_id(self, source_id: str):
        """Resolve a UI/command source_id without using row position or name."""
        with self._source_guard():
            return next((source for source in self.telegram_sources.values() if source.source_id == str(source_id)), None)

    def _source_by_chat_id(self, chat_id: str):
        with self._source_guard():
            return next((source for source in self.telegram_sources.values() if source.chat_id == str(chat_id)), None)

    def telegram_source_snapshot(self) -> dict[str, TelegramSource]:
        """Return a stable source mapping for workers and UI snapshots."""
        with self._source_guard():
            return {chat_id: replace(source) for chat_id, source in self.telegram_sources.items()}

    def _source_guard(self):
        """Provide compatibility for lightweight test composition roots."""
        lock = getattr(self, "_source_lock", None)
        if lock is None:
            self._source_lock = RLock()
            lock = self._source_lock
        return lock

    def _on_telegram_post(self, payload: dict) -> bool:
        """Store a source update before its global Telegram checkpoint advances."""
        if not self.telegram.running:
            return True
        chat_id = str(payload["chat"].get("id", ""))
        source = self._source_by_chat_id(chat_id)
        if source is None or not source.enabled:
            return True
        if source.pending_connection:
            self.add_telegram_source(replace(source, pending_connection=False))
        try:
            update_id = payload.get("update_id")
            if update_id is None:
                raise ValueError("Telegram update does not contain an ID")
            self.database.enqueue_telegram_update(int(update_id), payload)
            self.process_pending_telegram_sources()
            return True
        except Exception:
            self.database.record_telegram_source_error(chat_id, "intake_failed")
            self.database.log(f"telegram:{chat_id}", "telegram_source_intake", "failed", "")
            self.parsing_state = ProcessState.ERROR
            return False

    def _on_telegram_user_message(self, envelope, *, deliver: bool = True):
        """Run User API messages through the same filter, SQLite and notification path."""
        result = self.orders.intake_telegram_message(envelope)
        if deliver and result.is_new and result.matched:
            self._notify_new_order(result.order)
        return result

    def _telegram_delivery_executor_for_user_api(self) -> ThreadPoolExecutor:
        """Return the one serial Bot API sender shared by realtime and fill jobs."""
        executor = getattr(self, "_telegram_delivery_executor", None)
        if executor is None:
            lock = getattr(self, "_telegram_delivery_lock", None)
            if lock is None:
                lock = self._telegram_delivery_lock = RLock()
            with lock:
                executor = getattr(self, "_telegram_delivery_executor", None)
                if executor is None:
                    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="telegram-delivery")
                    self._telegram_delivery_executor = executor
        return executor

    def _telegram_fill_executor_for_user(self) -> ThreadPoolExecutor:
        """Return the single worker that plans finite SQLite delivery batches."""
        executor = getattr(self, "_telegram_fill_executor", None)
        if executor is None:
            lock = getattr(self, "_telegram_fill_lock", None)
            if lock is None:
                lock = self._telegram_fill_lock = RLock()
            with lock:
                executor = getattr(self, "_telegram_fill_executor", None)
                if executor is None:
                    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="telegram-fill")
                    self._telegram_fill_executor = executor
        return executor

    def _queue_user_api_delivery(self, order) -> None:
        """Put a saved User API order on the common Bot API delivery path."""
        self._notify_new_order(order)

    def _flush_user_api_delivery_batch(self, kind: str) -> None:
        """Run a finite SQLite fill after a History pass, through the shared sender."""
        if self.notifications_enabled:
            future = self._request_delivery_fill(f"{kind}_history", wait=False)
            if future is not None:
                future.result()

    def _request_delivery_fill(self, reason: str, *, user_id: int | None = None, wait: bool = False):
        """Coalesce fill requests so only one latest-70 query/send batch is active."""
        if not self.notifications_enabled:
            return None
        if user_id is not None and user_id not in self.telegram_transport.allowed_user_ids:
            return None
        lock = getattr(self, "_telegram_fill_lock", None)
        if lock is None:
            lock = self._telegram_fill_lock = RLock()
        with lock:
            future = getattr(self, "_telegram_fill_future", None)
            if future is None or future.done():
                future = self._telegram_fill_executor_for_user().submit(self._run_delivery_fill, reason, user_id)
                self._telegram_fill_future = future
                future.add_done_callback(lambda done: self._clear_delivery_fill(done))
            else:
                self.database.log("telegram", "delivery_fill", "coalesced", reason)
        if wait:
            return future.result()
        return future

    def _clear_delivery_fill(self, future) -> None:
        """Release the single-flight slot only after its current batch completes."""
        with self._telegram_fill_lock:
            if self._telegram_fill_future is future:
                self._telegram_fill_future = None

    def _run_delivery_fill(self, reason: str, user_id: int | None = None) -> int:
        """Deliver the newest 70 eligible Orders in old-to-new order via one sender."""
        if not self.notifications_enabled:
            return 0
        if reason in {"start", "orders_fallback", "desktop_startup"}:
            user_api = getattr(self, "telegram_user", None)
            wait_started = time.monotonic()
            while (user_api is not None and getattr(user_api, "delivery_barrier_active", False)
                   and not getattr(user_api, "history_collection_ready", False)
                   and time.monotonic() - wait_started < DELIVERY_BARRIER_MAX_SECONDS):
                time.sleep(0.1)
        recipients = [user_id] if user_id is not None else sorted(self.telegram_transport.allowed_user_ids)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=12)
        sent_count = skipped_count = failed_count = 0
        self.database.log("telegram", "delivery_fill", "started", f"reason={reason}")
        for owner_id in recipients:
            if owner_id not in self.telegram_transport.allowed_user_ids:
                continue
            candidates = self.database.list_recent_telegram_orders_for_ordered_delivery(cutoff, 70)
            self.database.log("telegram", "delivery_fill_selection", "ok", f"reason={reason} selected={len(candidates)}")
            for order in candidates:
                if not self.notifications_enabled:
                    break
                existing = self.database.telegram_order_delivery(order.id, owner_id)
                if existing is not None:
                    # sent and ambiguous sending reservations are both terminal for auto-retry.
                    skipped_count += 1
                    continue
                future = self._queue_order_delivery(order, owner_id, recheck_profile=False)
                if future is None:
                    skipped_count += 1
                    continue
                try:
                    if future.result():
                        sent_count += 1
                    else:
                        failed_count += 1
                except Exception:
                    failed_count += 1
                    self.database.log("telegram", "order_notification", "failed", "delivery_worker_error")
        self.database.log(
            "telegram", "delivery_fill", "ok" if not failed_count else "partial",
            f"reason={reason} sent={sent_count} skipped_sent_or_sending={skipped_count} failed={failed_count}",
        )
        self._drain_deferred_realtime_orders()
        return sent_count

    def _drain_deferred_realtime_orders(self) -> int:
        """Send matched realtime Orders held briefly behind a History delivery barrier."""
        if not self.notifications_enabled:
            return 0
        lock = getattr(self, "_telegram_delivery_lock", None)
        if lock is None:
            lock = self._telegram_delivery_lock = RLock()
        with lock:
            deferred_orders = getattr(self, "_deferred_delivery_orders", None)
            if deferred_orders is None:
                deferred_orders = self._deferred_delivery_orders = {}
            deferred, self._deferred_delivery_orders = deferred_orders, {}
        sent = 0
        for order in deferred.values():
            for owner_id in sorted(self.telegram_transport.allowed_user_ids):
                if self.database.telegram_order_delivery(order.id, owner_id) is not None:
                    continue
                # Persisted MATCHED is already the canonical filter result; this
                # deferred path must preserve fill semantics and avoid reclassifying it.
                future = self._queue_order_delivery(order, owner_id, recheck_profile=False)
                if future is not None:
                    try:
                        sent += int(bool(future.result()))
                    except Exception:
                        self.database.log("telegram", "order_notification", "failed", "deferred_delivery_error")
        if deferred:
            self.database.log("telegram", "deferred_realtime_delivery", "ok", f"orders={len(deferred)} sent={sent}")
        return sent

    def _queue_order_delivery(self, order, owner_id: int, *, recheck_profile: bool):
        """Queue one recipient attempt and collapse duplicate in-memory requests."""
        lock = getattr(self, "_telegram_delivery_lock", None)
        if lock is None:
            lock = self._telegram_delivery_lock = RLock()
        key = (str(order.id), str(owner_id))
        queued = getattr(self, "_telegram_delivery_queued", None)
        if queued is None:
            queued = self._telegram_delivery_queued = set()
        with lock:
            if key in queued:
                return None
            queued.add(key)
            try:
                future = self._telegram_delivery_executor_for_user_api().submit(
                    self._deliver_order_to_user, order, owner_id, recheck_profile=recheck_profile,
                )
            except Exception:
                queued.discard(key)
                raise
            future.add_done_callback(lambda _done, item=key: self._release_queued_delivery(item))
            return future

    def _release_queued_delivery(self, key: tuple[str, str]) -> None:
        """Forget a completed in-memory attempt; SQLite retains durable delivery state."""
        with self._telegram_delivery_lock:
            self._telegram_delivery_queued.discard(key)

    def _deliver_recent_orders(self, user_id: int) -> int:
        """Synchronous compatibility helper using the same ordered coordinator."""
        return self._run_delivery_fill("compatibility", user_id)

    @staticmethod
    def _restore_result(added: int) -> str:
        """Return a short response to /start or /orders recovery."""
        return f"Добавлено заказов: {added}." if added else "Новых заказов для добавления нет."

    def _deliver_order_to_user(self, order, owner_id: int, *, recheck_profile: bool = True) -> bool:
        """Reserve, send, and persist one recipient delivery using the established durable flow."""
        if not self.notifications_enabled:
            return False
        persisted = self.database.get_order(order.id)
        if persisted is None or persisted.status != OrderStatus.ACTIVE or persisted.classification != OrderClassification.MATCHED:
            return False
        if recheck_profile and not matches(persisted, self.orders.profile):
            return False
        # Резервируем доставку до вызова Telegram, чтобы параллельные пути не отправили две карточки.
        if not self.database.reserve_telegram_order_delivery(persisted.id, owner_id):
            return False
        try:
            reply = self.telegram_feed.new_order(owner_id, persisted)
            sender = getattr(self.telegram_transport, "send_message_result", None)
            if sender is None:
                accepted = self.telegram_transport.send_message(owner_id, reply.text, reply.reply_markup)
                result = {"ok": True, "result": {}} if accepted else {"ok": False}
            else:
                result = sender(owner_id, reply.text, reply.reply_markup)
            if not result or not result.get("ok"):
                self.database.release_telegram_order_delivery(persisted.id, owner_id)
                self.database.log("telegram", "order_notification", "failed", "")
                return False
            telegram_message_id = (result.get("result") or {}).get("message_id", "")
            self.database.complete_telegram_order_delivery(persisted.id, owner_id, telegram_message_id)
            return True
        except Exception:
            # A transport exception can happen after Telegram accepted the message.
            # Keep the durable sending reservation to prevent a duplicate on retry/restart.
            self.database.log("telegram", "order_notification", "failed", "")
            return False

    def process_pending_telegram_sources(self, now: datetime | None = None) -> None:
        """Process each due source independently from the bot-wide receive checkpoint."""
        if not self.telegram.running:
            return
        for source in self.telegram_source_snapshot().values():
            if not source.enabled or not self.database.telegram_source_due(source, now):
                continue
            pending = self.database.pending_telegram_updates(source.chat_id)
            if not pending:
                continue
            self.parsing_state = ProcessState.RUNNING
            try:
                for update_id, payload in pending:
                    result = self.orders.intake_telegram_post(payload)
                    self.database.mark_telegram_update_processed(update_id)
                    if result.is_new and result.matched:
                        self._notify_new_order(result.order)
                self.database.mark_telegram_source_processed(source.chat_id, now)
                self.database.log(f"telegram:{source.chat_id}", "telegram_source_processed", "ok", "")
            except Exception:
                # The inbox record remains pending and is retried on a later poll.
                self.database.record_telegram_source_error(source.chat_id, "processing_failed")
                self.database.log(f"telegram:{source.chat_id}", "telegram_source_processed", "failed", "")
            finally:
                self.parsing_state = ProcessState.WAITING

    def _notify_new_order(self, order) -> None:
        """Deliver only a persisted order that remains matched by the active profile."""
        if not self.notifications_enabled:
            return
        user_api = getattr(self, "telegram_user", None)
        if user_api is not None and getattr(user_api, "delivery_barrier_active", False):
            lock = getattr(self, "_telegram_delivery_lock", None)
            if lock is None:
                lock = self._telegram_delivery_lock = RLock()
            with lock:
                deferred = getattr(self, "_deferred_delivery_orders", None)
                if deferred is None:
                    deferred = self._deferred_delivery_orders = {}
                deferred[str(order.id)] = order
            return
        for owner_id in self.telegram_transport.allowed_user_ids:
            self._queue_order_delivery(order, owner_id, recheck_profile=True)

    def _pause_command(self, _user: int, _args: str) -> str:
        self.telegram.running = False
        self.parsing_state = ProcessState.PAUSED
        return "Поиск и отправка приостановлены."

    def _resume_command(self, _user: int, _args: str) -> str:
        self.telegram.running = True
        self.parsing_state = ProcessState.RUNNING
        return "Поиск и отправка возобновлены."

    def accounts_text(self) -> str:
        return f"Аккаунтов сохранено: {len(self.accounts.all())}\nАктивно: {len(self.accounts.active())}/{self.accounts.max_active}"

    def orders_text(self) -> str:
        orders = self.database.list_orders(10)
        if not orders:
            return "Найденных заказов пока нет."
        return "\n".join(f"{order.id} — {order.title}" for order in orders)

    def _draft_command(self, _user: int, args: str) -> str:
        order_id = args.split()[0] if args else ""
        order = self.database.get_order(order_id)
        if order is None:
            return "Использование: /draft ID_заказа"
        proposal_text = self.proposals.generate(order, self.proposal_profile)
        job = DispatchJob(
            uuid4().hex, order.id, "manual", ProposalMode.DRAFT,
            status=JobStatus.AWAITING_CONFIRMATION, proposal_text=proposal_text,
            idempotency_key=uuid4().hex,
        )
        self.queue.enqueue(job)
        return f"Черновик создан для заказа {order.id}:\n\n{proposal_text}"
