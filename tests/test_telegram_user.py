import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from freelance_bot.accounts.manager import AccountManager
from freelance_bot.core.models import OrderClassification, OrderStatus, SearchProfile, TelegramMessageEnvelope, TelegramSource
from freelance_bot.core.order_service import OrderService
from freelance_bot.storage.database import Database
from freelance_bot.telegram_bot.feed import TelegramOrderFeed
from freelance_bot.telegram_user.adapter import SourceAccessError, TelegramUserAdapter, normalize_reference
from freelance_bot.telegram_user.mapper import envelope_from_telethon
from freelance_bot.telegram_user.service import TelegramUserService


class Channel:
    broadcast = True
    title = "Python jobs"
    username = "python_jobs"


class User:
    id = 44
    username = "client_dev"
    first_name = "Ivan"
    last_name = "Petrov"


class Message:
    chat_id = -1001234567890
    id = 17
    message = "Нужен Python-разработчик\nКонтакт для связи: @client_dev"
    date = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    post_author = ""


class FakeSecretStore:
    def encrypt(self, _value):
        return "sealed-session"

    def decrypt(self, _value):
        return "{}"


class FakeSession:
    pass


class FakeStringSession:
    def __init__(self, _value=""):
        pass

    @staticmethod
    def save(_session):
        return "session-value"


class FakeClient:
    def __init__(self, _session, _api_id, _api_hash):
        self.session = FakeSession()
        self.connected = False

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def send_code_request(self, _phone):
        return None

    async def sign_in(self, **_kwargs):
        return None

    async def get_me(self):
        return type("TelegramUser", (), {"id": 900, "username": "owner"})()

    def add_event_handler(self, *_args):
        return None

    def is_connected(self):
        return self.connected


class FakeEvents:
    class NewMessage:
        pass


class FakeHistoryAdapter:
    """Capture history parameters without using a live Telethon account."""

    def __init__(self) -> None:
        self.calls = []

    async def history(self, source, after_message_id="", hours=2, until=None):
        self.calls.append((source.chat_id, after_message_id, hours, until))
        if False:
            yield None


class CountingHistoryAdapter:
    """Measure concurrent history calls without connecting to Telegram."""

    def __init__(self, failing_chat_id="") -> None:
        self.active = 0
        self.max_active = 0
        self.completed = []
        self.failing_chat_id = failing_chat_id

    async def history(self, source, after_message_id="", hours=2, until=None):
        if source.chat_id == self.failing_chat_id:
            raise SourceAccessError("forbidden", "test source failure")
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.005)
            self.completed.append(source.chat_id)
            if False:
                yield None
        finally:
            self.active -= 1


class PipelineHistoryAdapter:
    """Small synthetic adapter for queue and startup-history regression tests."""

    def __init__(self, messages_by_chat=None, delay=0.0) -> None:
        self.messages_by_chat = messages_by_chat or {}
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self.calls = []

    async def history(self, source, hours=1, until=None, limit=100):
        self.calls.append((source.chat_id, hours, until, limit))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            for envelope in self.messages_by_chat.get(source.chat_id, []):
                yield envelope
        finally:
            self.active -= 1


def _pipeline_envelope(chat_id: str, message_id: int, published_at=None):
    return TelegramMessageEnvelope(
        chat_id, str(message_id), "Нужно сделать Python-бота", "Тест", "channel", "test",
        published_at or datetime.now(timezone.utc),
    )


class HistoryMessage:
    def __init__(self, message_id: int, date: datetime) -> None:
        self.chat_id = -1001234567890
        self.id = message_id
        self.message = f"Python-заказ {message_id}"
        self.date = date
        self.post_author = ""

    async def get_sender(self):
        return User()


class HistoryClient:
    def __init__(self, messages) -> None:
        self.messages = messages

    async def get_entity(self, _reference):
        return Channel()

    async def iter_messages(self, _entity, min_id=0):
        for message in self.messages:
            if message.id > min_id:
                yield message


def test_user_mapper_keeps_telegram_identity_author_and_text() -> None:
    envelope = envelope_from_telethon(Message(), Channel(), User())
    assert envelope.chat_id == "-1001234567890"
    assert envelope.message_id == "17"
    assert envelope.chat_username == "python_jobs"
    assert envelope.author_user_id == 44
    assert envelope.author_username == "client_dev"
    assert envelope.author_entity_type == "user"
    assert "\n" in envelope.text


def test_telethon_user_without_username_keeps_id_for_deep_link() -> None:
    no_username_user = type("User", (), {
        "id": 45, "username": None, "first_name": "Иван", "last_name": "", "bot": False, "deleted": False,
    })()
    message = SimpleNamespace(
        chat_id=-1001234567890, id=201, message="Нужно сделать Python-проект", date=datetime.now(timezone.utc), post_author="",
    )
    envelope = envelope_from_telethon(message, Channel(), no_username_user)
    database = Database(":memory:")
    order = OrderService(database).intake_telegram_message(envelope).order
    buttons = [button for row in (TelegramOrderFeed(database).new_order(7, order).reply_markup or {}).get("inline_keyboard", []) for button in row]
    assert envelope.author_entity_type == "user"
    assert order.metadata["author_contact_url"] == "tg://user?id=45"
    assert next(button["url"] for button in buttons if button["text"] == "✉️ Написать") == "tg://user?id=45"
    database.close()


def test_telethon_channel_sender_and_deleted_user_are_not_contacts() -> None:
    database = Database(":memory:")
    message = SimpleNamespace(
        chat_id=-1001234567890, id=202, message="Нужно сделать Python-проект", date=datetime.now(timezone.utc), post_author="",
    )
    channel_sender = type("Channel", (), {"id": 999, "username": "source_channel", "title": "Source"})()
    channel_envelope = envelope_from_telethon(message, Channel(), channel_sender)
    channel_order = OrderService(database).intake_telegram_message(channel_envelope).order
    buttons = [button for row in (TelegramOrderFeed(database).new_order(7, channel_order).reply_markup or {}).get("inline_keyboard", []) for button in row]
    assert channel_envelope.author_entity_type == "sender_chat"
    assert not any(button["text"] == "✉️ Написать" for button in buttons)

    deleted_user = type("User", (), {
        "id": 46, "username": "gone_user", "first_name": "Deleted", "last_name": "", "bot": False, "deleted": True,
    })()
    deleted_envelope = envelope_from_telethon(message, Channel(), deleted_user)
    deleted_order = OrderService(database).intake_telegram_message(deleted_envelope).order
    deleted_buttons = [button for row in (TelegramOrderFeed(database).new_order(7, deleted_order).reply_markup or {}).get("inline_keyboard", []) for button in row]
    assert deleted_envelope.author_entity_type == "deleted_user"
    assert not any(button["text"] == "✉️ Написать" for button in deleted_buttons)
    database.close()


def test_user_api_message_uses_existing_filter_storage_and_direct_links() -> None:
    database = Database(":memory:")
    service = OrderService(database, SearchProfile("p", "p", ["python"], []))
    envelope = TelegramMessageEnvelope(
        "-1001234567890", "17", "Нужен разработчик Python. Нужно сделать Telegram-бота.\nКонтакт для связи: @client_dev",
        "Python jobs", "channel", "python_jobs", datetime.now(timezone.utc), 44, "client_dev", "Ivan",
    )
    result = service.intake_telegram_message(envelope)
    order = database.get_order(result.order.id)
    assert result.is_new is True
    assert order.external_id == "-1001234567890:17"
    assert order.url == "https://t.me/python_jobs/17"
    assert order.metadata["declared_contact_url"] == "https://t.me/client_dev"
    labels = {button["text"] for row in (TelegramOrderFeed(database).command(7, "")[0].reply_markup or {}).get("inline_keyboard", []) for button in row}
    assert "✉️ Написать" in labels
    assert "🔗 Оригинал" in labels
    database.close()


def test_bot_and_user_repeats_do_not_unarchive_or_reclassify_order() -> None:
    database = Database(":memory:")
    service = OrderService(database, SearchProfile("p", "p", ["python"], []))
    first = service.intake_telegram_message(TelegramMessageEnvelope(
        "-1009", "21", "Нужно сделать Python-проект", "Чат", "supergroup", "", None,
    ))
    database.archive_order(first.order.id)
    duplicate = service.intake_telegram_message(TelegramMessageEnvelope(
        "-1009", "21", "Нужно сделать Python-проект", "Чат", "supergroup", "jobs_feed", None, 51, "verified_user", "User",
    ))
    saved = database.get_order(first.order.id)
    assert duplicate.is_new is False
    assert saved.status == OrderStatus.ARCHIVED
    assert saved.classification == OrderClassification.MATCHED
    assert saved.metadata["author_user_id"] == 51
    assert saved.url == "https://t.me/c/9/21"
    assert saved.metadata["original_message_url"] == "https://t.me/jobs_feed/21"
    database.close()


def test_user_source_metadata_and_checkpoint_are_persisted() -> None:
    database = Database(":memory:")
    source = TelegramSource("-1001234567890", "Закрытый канал", True, "ru", 120, False, "https://t.me/+invite", "user", "available")
    database.save_telegram_source(source)
    database.mark_telegram_user_message_saved(source.chat_id, "31")
    database.mark_telegram_user_history_synced(source.chat_id)
    loaded = database.load_telegram_sources()[0]
    checkpoint = database.telegram_user_checkpoint(source.chat_id)
    assert loaded.access_mode == "user"
    assert loaded.reference == "https://t.me/+invite"
    assert checkpoint["last_message_id"] == "31"
    assert checkpoint["history_synced_at"]
    database.close()


def test_new_user_source_keeps_a_fixed_one_hour_initial_window() -> None:
    database = Database(":memory:")
    completed = datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc)
    database.begin_telegram_user_initial_sync("-1001", completed)
    checkpoint = database.telegram_user_checkpoint("-1001")
    assert checkpoint["initial_history_from_at"] == (completed - timedelta(hours=1)).isoformat()
    assert checkpoint["initial_history_until_at"] == completed.isoformat()
    database.close()


def test_initial_sync_uses_bounded_hour_without_checkpoint_replay() -> None:
    database = Database(":memory:")
    accounts = AccountManager(database=database)
    sources = {"-1001": TelegramSource("-1001", "Тест", reference="@test", access_mode="user")}
    service = TelegramUserService(database, accounts, lambda: sources, lambda _source: None,
                                  lambda _message: SimpleNamespace(is_new=False, matched=False))
    adapter = FakeHistoryAdapter()
    service._adapter = adapter
    service.running = True
    completed = datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc)
    database.begin_telegram_user_initial_sync("-1001", completed)
    # Realtime checkpoints do not enlarge the short startup history window.
    database.mark_telegram_user_message_saved("-1001", "9")
    asyncio.run(service._sync_source(sources["-1001"]))
    assert adapter.calls[0][1:] == ("", 1, None)
    assert database.telegram_user_checkpoint("-1001")["history_synced_at"]
    database.mark_telegram_user_message_saved("-1001", "10")
    asyncio.run(service._sync_source(sources["-1001"]))
    assert adapter.calls[1][1:] == ("", 1, None)
    service.stop()
    database.close()


def test_removed_source_cannot_start_a_queued_sync() -> None:
    database = Database(":memory:")
    accounts = AccountManager(database=database)
    source = TelegramSource("-1001", "Тест", reference="@test", access_mode="user", source_id="stable-1")
    sources = {source.chat_id: source}
    service = TelegramUserService(database, accounts, lambda: dict(sources), lambda _source: None,
                                  lambda _message: SimpleNamespace(is_new=False, matched=False))
    adapter = FakeHistoryAdapter()
    service._adapter = adapter
    service.running = True
    sources.clear()

    asyncio.run(service._sync_source(source))

    assert adapter.calls == []
    assert database.telegram_user_checkpoint(source.chat_id)["history_synced_at"] == ""
    service.stop()
    database.close()


def test_one_event_loop_limits_history_concurrency_for_100_sources() -> None:
    database = Database(":memory:")
    accounts = AccountManager(database=database)
    sources = {
        f"-100{i}": TelegramSource(f"-100{i}", "", True, "ru", 120, False, "", "user", "available", f"source-{i}")
        for i in range(100)
    }
    service = TelegramUserService(database, accounts, lambda: dict(sources), lambda _source: None,
                                  lambda _message: SimpleNamespace(is_new=False, matched=False))
    adapter = CountingHistoryAdapter()
    service._adapter = adapter
    service.running = True

    async def run_all():
        await asyncio.gather(*(service._sync_source(source) for source in sources.values()))

    asyncio.run(run_all())

    assert adapter.max_active <= 4
    assert set(adapter.completed) == set(sources)
    assert all(database.telegram_user_checkpoint(chat_id)["history_synced_at"] for chat_id in sources)
    service.stop()
    database.close()


def test_one_source_failure_does_not_block_other_bounded_syncs() -> None:
    database = Database(":memory:")
    accounts = AccountManager(database=database)
    sources = {
        f"-100{i}": TelegramSource(f"-100{i}", "", True, "ru", 120, False, "", "user", "available", f"source-{i}")
        for i in range(6)
    }

    def save_source(source):
        sources[source.chat_id] = source

    service = TelegramUserService(database, accounts, lambda: dict(sources), save_source,
                                  lambda _message: SimpleNamespace(is_new=False, matched=False))
    adapter = CountingHistoryAdapter("-1000")
    service._adapter = adapter
    service.running = True

    async def run_all():
        await asyncio.gather(*(service._sync_source(source) for source in sources.values()))

    asyncio.run(run_all())

    assert set(adapter.completed) == set(sources) - {"-1000"}
    assert sources["-1000"].access_status == "forbidden"
    service.stop()
    database.close()


def test_initial_history_keeps_only_messages_inside_the_one_hour_window() -> None:
    end = datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc)
    client = HistoryClient([
        HistoryMessage(4, end + timedelta(seconds=1)),
        HistoryMessage(3, end - timedelta(hours=1)),
        HistoryMessage(2, end - timedelta(hours=1, seconds=1)),
    ])
    adapter = TelegramUserAdapter(client)
    source = TelegramSource("-1001234567890", "Python jobs", reference="@python_jobs", access_mode="user")

    async def collect():
        return [item async for item in adapter.history(source, hours=1, until=end)]

    messages = asyncio.run(collect())
    assert [item.message_id for item in messages] == ["3"]


def test_startup_history_is_bounded_to_one_hour_and_100_messages() -> None:
    database = Database(":memory:")
    accounts = AccountManager(database=database)
    source = TelegramSource("-1001", "Тест", reference="@test", access_mode="user")
    now = datetime.now(timezone.utc)
    recent = [_pipeline_envelope(source.chat_id, item, now - timedelta(minutes=1)) for item in range(150)]
    old = _pipeline_envelope(source.chat_id, 999, now - timedelta(minutes=61))
    received = []
    service = TelegramUserService(database, accounts, lambda: {source.chat_id: source}, lambda _source: None,
                                  lambda envelope: received.append(envelope) or SimpleNamespace(is_new=False, matched=False))
    service._adapter = PipelineHistoryAdapter({source.chat_id: [*recent, old]})
    service.running = True

    async def run():
        await service._start_pipeline()
        await service._start_history_pass([source], "startup")
        await asyncio.gather(*list(service._history_tasks))
        await service._stop_pipeline()

    asyncio.run(run())
    assert len(received) == 100
    assert all(item.message_id != "999" for item in received)
    database.close()


def test_startup_history_uses_shared_six_worker_pool_and_isolates_sources() -> None:
    database = Database(":memory:")
    accounts = AccountManager(database=database)
    sources = {f"-100{item}": TelegramSource(f"-100{item}", access_mode="user", source_id=f"source-{item}")
               for item in range(12)}
    adapter = PipelineHistoryAdapter(delay=0.002)
    service = TelegramUserService(database, accounts, lambda: dict(sources), lambda _source: None,
                                  lambda _message: SimpleNamespace(is_new=False, matched=False))
    service._adapter = adapter
    service.running = True

    async def run():
        await service._start_pipeline()
        await service._start_history_pass(list(sources.values()), "startup")
        await asyncio.gather(*list(service._history_tasks))
        await service._stop_pipeline()

    asyncio.run(run())
    assert adapter.max_active <= 3  # shared account-level request limiter
    assert service.metrics["sources_processed"] == len(sources)
    database.close()


def test_realtime_is_processed_while_startup_history_is_waiting() -> None:
    database = Database(":memory:")
    accounts = AccountManager(database=database)
    source = TelegramSource("-1001", "Тест", reference="@test", access_mode="user", source_id="source-1")
    received = []
    service = TelegramUserService(database, accounts, lambda: {source.chat_id: source}, lambda _source: None,
                                  lambda envelope: received.append(envelope.message_id) or SimpleNamespace(is_new=False, matched=False))
    service._adapter = PipelineHistoryAdapter({source.chat_id: [_pipeline_envelope(source.chat_id, 1)]}, delay=0.05)
    service.running = True

    async def run():
        await service._start_pipeline()
        await service._start_history_pass([source], "startup")
        await service._enqueue_realtime(source, _pipeline_envelope(source.chat_id, 2))
        await asyncio.sleep(0.02)
        assert "2" in received
        await asyncio.gather(*list(service._history_tasks))
        await service._stop_pipeline()

    asyncio.run(run())
    database.close()


def test_realtime_history_duplicates_use_the_existing_order_deduplication() -> None:
    database = Database(":memory:")
    accounts = AccountManager(database=database)
    source = TelegramSource("-1001", "Тест", reference="@test", access_mode="user", source_id="source-1")
    order_service = OrderService(database, SearchProfile("p", "p", ["python"], []))
    service = TelegramUserService(database, accounts, lambda: {source.chat_id: source}, lambda _source: None,
                                  order_service.intake_telegram_message)
    duplicate = _pipeline_envelope(source.chat_id, 77)
    service._adapter = PipelineHistoryAdapter({source.chat_id: [duplicate]})
    service.running = True

    async def run():
        await service._start_pipeline()
        await service._start_history_pass([source], "startup")
        await service._enqueue_realtime(source, duplicate)
        await asyncio.gather(*list(service._history_tasks))
        await asyncio.sleep(0.02)
        await service._stop_pipeline()

    asyncio.run(run())
    assert database.count("orders") == 1
    database.close()


def test_temporary_realtime_overflow_is_minimal_and_is_replayed_before_history() -> None:
    database = Database(":memory:")
    accounts = AccountManager(database=database)
    source = TelegramSource("-1001", "Тест", reference="@test", access_mode="user", source_id="source-1")
    received = []
    service = TelegramUserService(database, accounts, lambda: {source.chat_id: source}, lambda _source: None,
                                  lambda envelope: received.append(envelope.message_id) or SimpleNamespace(is_new=False, matched=False))
    service.running = True
    item = service._serialize_overflow(type("Item", (), {
        "source": source, "envelope": _pipeline_envelope(source.chat_id, 88),
    })())
    assert set(item) == {"source", "envelope"}
    assert "message" not in item["envelope"]

    async def run():
        await service._start_pipeline()
        await service._write_overflow(service._deserialize_overflow(item))
        await service._recover_overflow_into_realtime()
        await asyncio.sleep(0.05)
        await service._stop_pipeline()

    asyncio.run(run())
    assert received == ["88"]
    assert not service._overflow_path.exists()
    database.close()


def test_recovery_history_uses_disconnect_gap_with_two_minute_overlap() -> None:
    database = Database(":memory:")
    accounts = AccountManager(database=database)
    source = TelegramSource("-1001", "Тест", reference="@test", access_mode="user", source_id="source-1")
    now = datetime.now(timezone.utc)
    received = []
    service = TelegramUserService(database, accounts, lambda: {source.chat_id: source}, lambda _source: None,
                                  lambda envelope: received.append(envelope.message_id) or SimpleNamespace(is_new=False, matched=False))
    service._adapter = PipelineHistoryAdapter({source.chat_id: [
        _pipeline_envelope(source.chat_id, 1, now - timedelta(minutes=8)),
        _pipeline_envelope(source.chat_id, 2, now - timedelta(minutes=10)),
    ]})
    service.running = True
    service._last_disconnect_at = now - timedelta(minutes=7)

    async def run():
        await service._start_pipeline()
        await service._run_recovery_history()
        await asyncio.gather(*list(service._history_tasks))
        await service._stop_pipeline()

    asyncio.run(run())
    assert received == ["1"]
    assert service.metrics["recovery_count"] == 1
    database.close()


def test_source_reference_validation_never_turns_an_invite_into_join() -> None:
    assert normalize_reference("@python_jobs") == "@python_jobs"
    assert normalize_reference("https://t.me/python_jobs") == "@python_jobs"
    assert normalize_reference("https://t.me/+invite_code") == "https://t.me/+invite_code"
    try:
        normalize_reference("https://example.test/private")
    except SourceAccessError as exc:
        assert exc.code == "invalid_reference"
    else:
        raise AssertionError("unsupported source reference was accepted")


def test_user_login_encrypts_session_without_persisting_phone_or_code(monkeypatch) -> None:
    database = Database(":memory:")
    accounts = AccountManager(database=database)
    service = TelegramUserService(database, accounts, lambda: {}, lambda _source: None, lambda _message: None, FakeSecretStore())
    monkeypatch.setattr(
        TelegramUserService,
        "_telethon",
        staticmethod(lambda: (FakeClient, FakeEvents, RuntimeError, RuntimeError, FakeStringSession)),
    )
    challenge = service.begin_login(12345, "api-hash", "+12025550112")
    assert service.complete_code(challenge, "12345") == "authorized"
    account = accounts.get("telegram-user:900")
    assert account is not None
    assert account.name.endswith("0112 (@owner)")
    assert database.load_secret(account.id) == "sealed-session"
    assert database.get_setting("telegram_user_api_id") == "12345"
    service.forget()
    assert accounts.get("telegram-user:900") is None
    assert database.load_secret("telegram-user:900") == ""
    database.close()


def test_manual_history_uses_existing_pipeline_and_bounded_one_hour_window() -> None:
    database = Database(":memory:")
    accounts = AccountManager(database=database)
    source = TelegramSource("-1001", "Тест", reference="@test", access_mode="user", source_id="source-1")
    adapter = PipelineHistoryAdapter()
    flushes = []
    service = TelegramUserService(
        database, accounts, lambda: {source.chat_id: source}, lambda _source: None,
        lambda _message: SimpleNamespace(is_new=False, matched=False),
        flush_deliveries=flushes.append,
    )
    service._adapter = adapter
    service.running = True

    async def run():
        await service._start_pipeline()
        assert await service._request_manual_history() == "scheduled"
        await asyncio.gather(*list(service._history_tasks))
        assert len(adapter.calls) == 1
        assert adapter.calls[0][0] == source.chat_id
        assert adapter.calls[0][1] == 1
        assert adapter.calls[0][3] == 100
        assert flushes == ["manual"]
        assert await service._request_manual_history() == "cooldown"
        await service._stop_pipeline()

    asyncio.run(run())
    database.close()


def test_repeated_manual_history_joins_one_active_scan() -> None:
    database = Database(":memory:")
    accounts = AccountManager(database=database)
    source = TelegramSource("-1001", "Тест", reference="@test", access_mode="user", access_status="available",
                            source_id="source-1")
    adapter = PipelineHistoryAdapter(delay=0.05)
    service = TelegramUserService(
        database, accounts, lambda: {source.chat_id: source}, lambda _source: None,
        lambda _message: SimpleNamespace(is_new=False, matched=False),
    )
    service._adapter = adapter
    service.running = True

    async def run():
        await service._start_pipeline()
        assert await service._request_manual_history() == "scheduled"
        assert await service._request_manual_history() == "joined"
        assert await service._request_manual_history() == "joined"
        await asyncio.gather(*list(service._history_tasks))
        assert len(adapter.calls) == 1
        await service._stop_pipeline()

    asyncio.run(run())
    database.close()


def test_manual_history_joins_startup_instead_of_starting_a_second_scan() -> None:
    database = Database(":memory:")
    accounts = AccountManager(database=database)
    source = TelegramSource("-1001", "Тест", reference="@test", access_mode="user", access_status="available",
                            source_id="source-1")
    adapter = PipelineHistoryAdapter(delay=0.05)
    service = TelegramUserService(
        database, accounts, lambda: {source.chat_id: source}, lambda _source: None,
        lambda _message: SimpleNamespace(is_new=False, matched=False),
    )
    service._adapter = adapter
    service.running = True

    async def run():
        await service._start_pipeline()
        assert await service._start_history_pass([source], "startup") == "scheduled"
        assert await service._request_manual_history() == "joined"
        await asyncio.gather(*list(service._history_tasks))
        assert len(adapter.calls) == 1
        await service._stop_pipeline()

    asyncio.run(run())
    database.close()


def test_manual_history_during_recovery_queues_one_full_pass_after_recovery() -> None:
    database = Database(":memory:")
    accounts = AccountManager(database=database)
    source = TelegramSource("-1001", "Тест", reference="@test", access_mode="user", access_status="available",
                            source_id="source-1")
    adapter = PipelineHistoryAdapter(delay=0.03)
    flushes = []
    service = TelegramUserService(
        database, accounts, lambda: {source.chat_id: source}, lambda _source: None,
        lambda _message: SimpleNamespace(is_new=False, matched=False),
        flush_deliveries=flushes.append,
    )
    service._adapter = adapter
    service.running = True

    async def run():
        await service._start_pipeline()
        now = datetime.now(timezone.utc)
        assert await service._start_history_pass(
            [source], "recovery", now - timedelta(minutes=5), now,
        ) == "scheduled"
        assert await service._request_manual_history() == "queued"
        while service._active_history_task is not None:
            await service._active_history_task
        assert len(adapter.calls) == 2
        assert flushes == ["recovery", "manual"]
        await service._stop_pipeline()

    asyncio.run(run())
    database.close()


def test_slow_history_releases_delivery_barrier_and_realtime_keeps_moving(monkeypatch) -> None:
    import freelance_bot.telegram_user.service as service_module

    monkeypatch.setattr(service_module, "DELIVERY_BARRIER_MAX_SECONDS", 0.01)
    database = Database(":memory:")
    accounts = AccountManager(database=database)
    source = TelegramSource("-1001", "Тест", reference="@test", access_mode="user", access_status="available",
                            source_id="source-1")
    delivered, flushes = [], []
    service = TelegramUserService(
        database, accounts, lambda: {source.chat_id: source}, lambda _source: None,
        lambda _message: SimpleNamespace(is_new=True, matched=True, order=SimpleNamespace(id="fresh")),
        deliver_order=lambda order: delivered.append(order.id),
        flush_deliveries=flushes.append,
    )
    service._adapter = PipelineHistoryAdapter(delay=0.1)
    service.running = True

    async def run():
        await service._start_pipeline()
        await service._start_history_pass([source], "manual")
        await service._enqueue_realtime(source, _pipeline_envelope(source.chat_id, 2))
        await asyncio.sleep(0.05)
        assert delivered == ["fresh"]
        assert "barrier_timeout" in flushes
        assert service.delivery_barrier_active is False
        await asyncio.gather(*list(service._history_tasks))
        await service._stop_pipeline()

    asyncio.run(run())
    database.close()
