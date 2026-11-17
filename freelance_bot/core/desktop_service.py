"""Application boundary for the Qt desktop, independent of Qt widgets."""

import json
import re
import webbrowser
from dataclasses import replace
from datetime import datetime
from urllib.parse import urlparse
from uuid import uuid4

from freelance_bot.core.models import Account, OrderClassification, OrderStatus, ProcessState, SearchProfile, ServiceCategory, TelegramSource
from freelance_bot.core.source_import import ImportProgress, ImportResult, SourceImportService


JOB_LABELS = {
    'pending': 'В очереди', 'preparing': 'Подготовка',
    'awaiting_confirmation': 'На проверке', 'running': 'В работе',
    'sending': 'Отправляется', 'sent': 'Отправлено', 'succeeded': 'Отправлено',
    'unknown_result': 'Результат неизвестен', 'failed': 'Ошибка',
    'paused': 'Пауза', 'cancelled': 'Отменено',
}

ACTION_LABELS = {
    'open_original': 'Открыт оригинал заказа',
    'order_archived': 'Заказ перенесён в архив',
    'order_restored': 'Заказ восстановлен из архива',
}
SERVICE_LABELS = {'system': 'Система'}
OPERATION_ID_RE = re.compile(r'^(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$', re.IGNORECASE)
MARKETPLACE_CATALOG = (
    {'id': 'kwork', 'name': 'Kwork', 'url': 'https://kwork.ru', 'builtin': True},
    {'id': 'fl.ru', 'name': 'FL.ru', 'url': 'https://fl.ru', 'builtin': True},
    {'id': 'freelance.ru', 'name': 'Freelance.ru', 'url': 'https://freelance.ru', 'builtin': True},
)


def feed_state(order, drafts, jobs):
    """Derive a display group without changing dispatch state."""
    if order.status != OrderStatus.ACTIVE:
        return 'archive', 'Архив'
    if order.classification == OrderClassification.UNDETERMINED:
        return 'undetermined', 'Не определено'
    if order.classification == OrderClassification.REJECTED:
        return 'rejected', 'Отклонён'
    job = jobs.get(order.id)
    if job:
        state = job['status']
        if state in ('sent', 'succeeded'):
            return 'sent', 'Отправлено'
        return 'review', JOB_LABELS.get(state, 'Требует проверки')
    if drafts.get(order.id, '').strip():
        return 'review', 'Черновик'
    return 'new', 'Новый'


def _journal_event(row):
    """Project a log row into fields that are safe to display in the desktop UI."""
    details = str(row['details'] or '').strip()
    action = str(row['action'] or '')
    account = str(row['account_id'] or '')
    return {
        'created_at': row['created_at'],
        'status': row['status'],
        'action': ACTION_LABELS.get(action, action.replace('_', ' ') or 'Не указано'),
        'service': SERVICE_LABELS.get(account, account or 'Система'),
        # Details can contain arbitrary source data. Only a standalone operation ID is safe for UI filtering.
        'operation_id': details if OPERATION_ID_RE.fullmatch(details) else '',
    }


class DesktopService:
    """Expose UI commands while keeping database and browser work in services."""

    def __init__(self, application):
        self.app = application
        self.deletion_service = getattr(self.app, 'deletion_service', None)
        soft = self.app.database.get_setting('desktop_memory_soft')
        hard = self.app.database.get_setting('desktop_memory_hard')
        try:
            if soft and hard and 256 <= int(soft) < int(hard) <= 5120:
                self.app.browser.memory_guard.soft_limit_mb = int(soft)
                self.app.browser.memory_guard.hard_limit_mb = int(hard)
        except ValueError:
            pass

    def snapshot(self):
        """Read a bounded feed and honest runtime status for background refresh."""
        orders = self.app.database.list_orders(2000, status=None)
        drafts, jobs = self.app.database.desktop_state()
        deletion_service = self._deletion_service()
        pending_deletions = {
            str(row['order_id']): row for row in deletion_service.list_pending_deletions()
        }
        items = [(order, *feed_state(order, drafts, jobs)) for order in orders]
        counts = {key: sum(group == key for _, group, _ in items) for key in ('new', 'undetermined', 'review', 'sent', 'archive')}
        configured = self.app.telegram.configured
        connected = self.app.telegram_transport.connected
        running = self.app.telegram.running
        user_api = getattr(self.app, 'telegram_user', None)
        telegram = 'подключён' if connected else ('подключается' if running and configured else 'не настроен' if not configured else 'нет соединения')
        parsing = 'работает' if running and connected else 'ожидание связи' if running else 'пауза'
        source_snapshot = getattr(self.app, 'telegram_source_snapshot', None)
        source_map = source_snapshot() if source_snapshot else dict(self.app.telegram_sources)
        return dict(items=items, drafts=drafts, jobs=jobs, counts=counts,
                    pending_deletions=pending_deletions,
                    telegram=telegram, parsing=parsing, running=running,
                    configured=configured, logs=[_journal_event(r) for r in self.app.database.list_logs(100)],
                    sources=[replace(source) for source in source_map.values()], accounts=self.app.accounts.all(),
                    source_states=self.app.database.telegram_source_states(),
                    telegram_user={
                        'configured': bool(user_api and user_api.configured),
                        'connected': bool(user_api and user_api.connected),
                        'status': 'подключён' if user_api and user_api.connected else (
                            'требует входа' if user_api and user_api.configured else 'не настроен'
                        ),
                    },
                    marketplaces=self._marketplace_snapshot(),
                    parsing_autostart=self.app.database.get_setting('parsing_start_on_launch', '1') == '1',
                    profile=replace(self.app.proposal_profile),
                    search_profile=replace(self.app.orders.profile),
                    limits=(self.app.browser.memory_guard.soft_limit_mb, self.app.browser.memory_guard.hard_limit_mb),
                    total=self.app.database.count('orders'))

    def save_draft(self, order_id, text):
        """Save editor content; never create an outgoing task."""
        self.app.database.save_desktop_draft(order_id, text)
        return order_id, text

    def save_search_profile(self, include_rules, exclude_rules, order_signals=None, vacancy_signals=None,
                            provider_ad_signals=None, stop_phrases=None, service_categories=None,
                            telegram_hard_stops=None, telegram_order_signals=None):
        """Persist the single local filtering profile and apply it to new intake."""
        current = self.app.orders.profile
        profile = SearchProfile(
            current.id,
            current.name,
            self._rule_list(include_rules),
            self._rule_list(exclude_rules),
            current.min_budget,
            current.max_budget,
            list(current.categories),
            self._rule_list(order_signals) if order_signals is not None else list(current.order_signals),
            self._rule_list(vacancy_signals) if vacancy_signals is not None else list(current.vacancy_signals),
            self._rule_list(provider_ad_signals) if provider_ad_signals is not None else list(current.provider_ad_signals),
            self._rule_list(stop_phrases) if stop_phrases is not None else list(current.stop_phrases),
            self._service_categories(service_categories) if service_categories is not None else list(current.service_categories),
            self._rule_list(telegram_hard_stops) if telegram_hard_stops is not None else list(current.telegram_hard_stops),
            self._rule_list(telegram_order_signals) if telegram_order_signals is not None else list(current.telegram_order_signals),
            current.telegram_gate_configured or telegram_hard_stops is not None or telegram_order_signals is not None,
        )
        self.app.database.save_search_profile(profile)
        self.app.orders.set_profile(profile)

    def restore_default_search_profile(self):
        """Restore editable local filtering defaults without touching saved orders."""
        from freelance_bot.core.filtering import default_search_profile
        profile = default_search_profile()
        self.app.database.save_search_profile(profile)
        self.app.orders.set_profile(profile)

    @classmethod
    def _service_categories(cls, value):
        """Convert UI category facts to models without exposing numeric weights."""
        result = []
        for item in value:
            if not isinstance(item, dict) or not str(item.get('name') or '').strip():
                continue
            result.append(ServiceCategory(
                str(item['name']).strip(), bool(item.get('enabled', True)),
                cls._rule_list(item.get('rules', '')), cls._rule_list(item.get('important_rules', '')),
            ))
        return result

    @staticmethod
    def _rule_list(value):
        """Accept one rule per line while tolerating pasted comma-separated lists."""
        rules = []
        seen = set()
        for item in str(value).replace(',', '\n').splitlines():
            rule = item.strip()
            key = rule.casefold()
            if rule and key not in seen:
                rules.append(rule)
                seen.add(key)
        return rules

    def generate(self, order_id):
        """Return the existing generator's text and truthful provider warnings."""
        order = self.app.database.get_order(order_id)
        if order is None:
            raise ValueError('Выберите существующий заказ.')
        return self.app.proposals.generate_result(order, self.app.proposal_profile)

    def open_original(self, order_id):
        """Open an explicit HTTP(S) order URL in the system browser."""
        order = self.app.database.get_order(order_id)
        if not order or urlparse(order.url).scheme not in ('http', 'https'):
            raise ValueError('Источник не предоставил ссылку на оригинал.')
        operation = uuid4().hex
        self.app.database.log('system', 'open_original', 'started', operation)
        opened = webbrowser.open(order.url)
        self.app.database.log('system', 'open_original', 'ok' if opened else 'failed', operation)
        if not opened:
            raise ValueError('Не удалось открыть браузер.')

    def archive(self, order_id, restore=False):
        """Archive or restore an order while protecting in-flight submissions."""
        _, jobs = self.app.database.desktop_state()
        job = jobs.get(order_id, {})
        if job.get('status') in ('pending', 'preparing', 'running', 'sending', 'unknown_result'):
            raise ValueError('Сначала разрешите выполняемую задачу или неизвестный результат отправки.')
        self.app.database.archive_order(order_id, OrderStatus.ACTIVE if restore else OrderStatus.ARCHIVED)
        self.app.database.log('system', 'order_restored' if restore else 'order_archived', 'ok', order_id)

    def schedule_deletion(self, order_ids):
        """Queue one or many archived Orders through the application deletion service."""
        return self._deletion_service().schedule_deletion(order_ids)

    def cancel_deletion(self, order_ids):
        """Cancel selected pending deletions while retaining the archived Orders."""
        return self._deletion_service().cancel_deletion(order_ids)

    def cancel_deletion_batch(self, batch_id):
        """Cancel the remaining entries in a previously scheduled bulk batch."""
        return self._deletion_service().cancel_deletion_batch(batch_id)

    def _deletion_service(self):
        """Resolve the app-owned deletion service for UI-independent business rules."""
        if self.deletion_service is None:
            from freelance_bot.core.deletion_service import DeletionService
            self.deletion_service = DeletionService(self.app.database)
        return self.deletion_service

    def archive_expired_orders(self):
        """Run the shared scheduled archive check without creating per-order timers."""
        check = getattr(self.app, 'archive_expired_orders', None)
        if check:
            return check()
        archived = self.app.database.archive_expired_orders()
        if archived:
            self.app.database.log('system', 'orders_auto_archived', 'ok', str(archived))
        return archived

    def toggle_parsing(self, running):
        """Pause intake without disconnecting Telegram control."""
        if running:
            self.app.start_services()
        else:
            self.app.telegram.running = False
            self.app.parsing_state = ProcessState.PAUSED

    def add_source(self, chat_id, name='', enabled=True, language='ru', poll_interval_seconds=120):
        """Add a source supported by the current bot transport."""
        if language not in {'ru', 'en'}:
            raise ValueError('Выберите поддерживаемый язык источника.')
        interval = int(poll_interval_seconds)
        if not 30 <= interval <= 86400:
            raise ValueError('Интервал должен быть от 30 секунд до 24 часов.')
        source, _resolved = self.app.add_telegram_source_reference(
            chat_id, name.strip(), bool(enabled), language, interval,
        )
        if not source.source_id:
            source = replace(source, source_id=f"telegram-{source.chat_id.replace('-', 'm').replace(':', 'x')}")
        return source

    def import_telegram_sources(self, path, progress: ImportProgress | None = None, cancel_event=None) -> ImportResult:
        """Run TXT import through the application's one canonical source pipeline."""
        importer = getattr(self.app, "source_importer", None) or SourceImportService(self.app)
        return importer.import_file(path, progress, cancel_event)

    def remove_source(self, source_id):
        """Remove a selected source through the application."""
        self.app.remove_telegram_source(source_id)

    def update_source(self, source_id, name, enabled, language, poll_interval_seconds):
        """Validate and persist editable Telegram source preferences."""
        source = self._find_source(source_id)
        if source is None:
            raise ValueError('Источник больше не существует. Обновите список.')
        if language not in {'ru', 'en'}:
            raise ValueError('Выберите поддерживаемый язык источника.')
        interval = int(poll_interval_seconds)
        if not 30 <= interval <= 86400:
            raise ValueError('Интервал должен быть от 30 секунд до 24 часов.')
        updated = replace(
            source,
            name=name.strip(),
            enabled=bool(enabled),
            language=language,
            poll_interval_seconds=interval,
        )
        self.app.add_telegram_source(updated)
        user_api = getattr(self.app, 'telegram_user', None)
        if user_api:
            user_api.source_changed(updated)
        return updated

    def set_source_enabled(self, source_id, enabled):
        """Persist a Telegram source switch without overwriting its pending form edits."""
        source = self._find_source(source_id)
        if source is None:
            raise ValueError('Источник больше не существует. Обновите список.')
        updated = replace(source, enabled=bool(enabled))
        self.app.add_telegram_source(updated)
        user_api = getattr(self.app, 'telegram_user', None)
        if user_api:
            user_api.source_changed(updated)
        return updated

    def _find_source(self, source_id):
        """Find by stable source_id, with legacy chat-id compatibility for old hosts."""
        resolver = getattr(self.app, '_source_by_id', None)
        if resolver:
            return resolver(source_id)
        return next((item for item in self.app.telegram_sources.values()
                     if item.source_id == str(source_id) or item.chat_id == str(source_id)), None)

    def begin_telegram_user_login(self, api_id, api_hash, phone):
        """Begin desktop-only User API authorization without exposing secret inputs."""
        return self.app.telegram_user.begin_login(api_id, api_hash, phone)

    def complete_telegram_user_code(self, challenge_id, code):
        """Submit a one-time Telegram code held only by the User API service."""
        return self.app.telegram_user.complete_code(challenge_id, code)

    def complete_telegram_user_password(self, challenge_id, password):
        """Submit a transient 2FA password and immediately clear it in the UI."""
        self.app.telegram_user.complete_password(challenge_id, password)

    def disconnect_telegram_user(self, forget=False):
        """Stop User API locally or revoke and erase its encrypted credentials."""
        if forget:
            self.app.telegram_user.forget()
        else:
            self.app.telegram_user.stop()

    def set_parsing_autostart(self, enabled):
        """Persist whether parsing services should start with the desktop application."""
        self.app.database.set_setting('parsing_start_on_launch', '1' if enabled else '0')

    def add_marketplace(self, name, url):
        """Save a marketplace target without claiming that an adapter exists."""
        name = name.strip()
        url = url.strip()
        parsed = urlparse(url)
        if not name:
            raise ValueError('Укажите название сайта или биржи.')
        if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
            raise ValueError('Укажите полный адрес сайта, например https://example.com.')
        entries = self._load_marketplaces()
        normalized = url.rstrip('/').casefold()
        if any(str(item.get('url', '')).rstrip('/').casefold() == normalized for item in entries):
            raise ValueError('Этот сайт уже добавлен.')
        entries.append({
            'id': 'custom-' + uuid4().hex,
            'name': name,
            'url': url.rstrip('/'),
            'enabled': False,
            'builtin': False,
        })
        self._save_marketplaces(entries)

    def set_marketplace_enabled(self, marketplace_id, enabled):
        """Persist an individual marketplace preference independently of adapter readiness."""
        entries = self._load_marketplaces()
        for item in entries:
            if item['id'] == marketplace_id:
                item['enabled'] = bool(enabled)
                self._save_marketplaces(entries)
                return
        raise ValueError('Источник больше не существует. Обновите список.')

    def remove_marketplace(self, marketplace_id):
        """Remove only a custom marketplace target; built-in targets remain available."""
        entries = self._load_marketplaces()
        target = next((item for item in entries if item['id'] == marketplace_id), None)
        if target is None:
            raise ValueError('Источник больше не существует. Обновите список.')
        if target.get('builtin'):
            raise ValueError('Встроенную биржу можно выключить, но нельзя удалить.')
        self._save_marketplaces([item for item in entries if item['id'] != marketplace_id])

    def _load_marketplaces(self):
        saved = self.app.database.get_setting('parsing_marketplaces', '')
        try:
            values = json.loads(saved) if saved else []
        except (TypeError, ValueError):
            values = []
        by_id = {
            str(item.get('id')): dict(item)
            for item in values
            if isinstance(item, dict) and item.get('id') and item.get('name') and item.get('url')
        }
        result = []
        for default in MARKETPLACE_CATALOG:
            item = {**default, 'enabled': False, **by_id.pop(default['id'], {})}
            item['builtin'] = True
            result.append(item)
        result.extend(item for item in by_id.values() if not item.get('builtin'))
        return result

    def _save_marketplaces(self, entries):
        safe = [
            {
                'id': str(item['id']),
                'name': str(item['name']),
                'url': str(item['url']),
                'enabled': bool(item.get('enabled')),
                'builtin': bool(item.get('builtin')),
            }
            for item in entries
        ]
        self.app.database.set_setting('parsing_marketplaces', json.dumps(safe, ensure_ascii=False))

    def _marketplace_snapshot(self):
        registry = getattr(self.app, 'sources', None)
        available = set(registry.platforms()) if registry and hasattr(registry, 'platforms') else set()
        result = []
        for item in self._load_marketplaces():
            ready = item['id'] in available
            if not item.get('enabled'):
                status, detail = 'выключен', 'Сбор отключён'
            elif ready:
                status, detail = 'требует проверки', 'Адаптер найден, нужна проверка аккаунта'
            else:
                status, detail = 'не настроено', 'Адаптер источника не подключён'
            result.append({**item, 'status': status, 'detail': detail})
        return result

    def add_account(self, name, platform):
        """Persist an unverified account without claiming integration support."""
        if not name.strip() or not platform.strip():
            raise ValueError('Укажите название и площадку.')
        self.app.accounts.add(Account(uuid4().hex, platform.strip(), name.strip(), requires_attention=True))

    def toggle_account(self, account_id):
        """Toggle the stored account preference without clearing verification."""
        account = self.app.accounts.get(account_id)
        if account:
            self.app.accounts.set_enabled(account_id, not account.enabled)

    def save_profile(self, skills, experience, examples, rules):
        """Save the existing profile format without changing generation semantics."""
        profile = replace(self.app.proposal_profile, skills=[s.strip() for s in skills.split(',') if s.strip()],
                          experience=experience, style_examples=[s.strip() for s in examples.split('\n---\n') if s.strip()], rules=rules)
        self.app.profile_store.save(profile)
        self.app.proposal_profile = profile

    def save_limits(self, soft, hard):
        """Apply and persist existing process memory limits."""
        if not 256 <= soft < hard <= 5120:
            raise ValueError('Лимиты: 256 ≤ мягкий < жёсткий ≤ 5120 МБ.')
        self.app.browser.memory_guard.soft_limit_mb = soft
        self.app.browser.memory_guard.hard_limit_mb = hard
        self.app.database.set_setting('desktop_memory_soft', str(soft))
        self.app.database.set_setting('desktop_memory_hard', str(hard))

    def shutdown(self):
        """Stop application services after pending editor writes complete."""
        self.app.shutdown()
