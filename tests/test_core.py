from datetime import datetime, timedelta, timezone
from dataclasses import replace
import tempfile
import os
from types import SimpleNamespace

from freelance_bot.accounts.manager import AccountManager
from freelance_bot.browser_automation.manager import MemoryGuard
from freelance_bot.core.filtering import classify, default_search_profile, matches, normalize_telegram_gate_text, telegram_order_gate
from freelance_bot.core.models import Account, AccountKind, DispatchJob, JobStatus, Order, OrderClassification, OrderStatus, ProposalMode, SearchProfile, TelegramMessageEnvelope, TelegramSource
from freelance_bot.core.order_service import OrderService
from freelance_bot.app import Application
from freelance_bot.core.models import ProcessState
from freelance_bot.storage.database import Database
from freelance_bot.telegram_bot.transport import TelegramPolling, TelegramReply
from freelance_bot.telegram_bot.feed import TelegramOrderFeed
from freelance_bot.scheduler.queue import DispatchQueue
from freelance_bot.telegram_bot.service import TelegramService
from freelance_bot.ai.prompt import PromptBuilder
from freelance_bot.core.models import ProposalProfile


def test_discovery_time_survives_storage_and_updates() -> None:
    database = Database(":memory:")
    original = datetime(2026, 1, 1, tzinfo=timezone.utc)
    order = Order("discovered", "test", "discovered", "Title", "", "", discovered_at=original)
    database.save_order(order)
    order.discovered_at = datetime.now(timezone.utc)
    database.save_order(order)
    assert database.get_order(order.id).discovered_at == original
    database.close()


def test_filter_matches_and_excludes() -> None:
    order = Order("1", "telegram", "x", "Python Telegram bot", "Нужен бот", "https://example.test", budget=100, category="dev")
    assert matches(order, SearchProfile("p", "p", ["python"], ["crypto"], 50, 200))
    assert not matches(order, SearchProfile("p", "p", ["design"], []))


def test_filter_classification_covers_all_approved_outcomes() -> None:
    order = Order("rules", "telegram", "rules:1", "Python bot", "Фриланс-заказ", "")
    assert classify(order, SearchProfile("p", "p", ["python", "bot"], ["вакансия"])).classification == OrderClassification.MATCHED
    assert classify(order, SearchProfile("p", "p", ["python"], ["bot", "заказ"])).classification == OrderClassification.REJECTED
    assert classify(order, SearchProfile("p", "p", ["python"], ["bot"])).classification == OrderClassification.REJECTED
    assert classify(order, SearchProfile("p", "p", ["дизайн"], ["вакансия"])).classification == OrderClassification.UNDETERMINED


def test_telegram_order_gate_normalizes_and_hard_stop_always_wins() -> None:
    assert normalize_telegram_gate_text("ИЩЕМ   В КОМАНДУ!!!") == "ищем в команду"
    result = telegram_order_gate(
        "Вакансия. Ищем Python разработчика в команду для разработки Telegram-бота. Зарплата 200000."
    )
    assert result.passed is False
    assert result.reason == "hard_stop:vacancy"


def test_telegram_order_gate_accepts_requested_freelance_work() -> None:
    messages = (
        "Нужно сделать лендинг для юридической компании. Бюджет 30000.",
        "Ищу Python разработчика. Нужно доработать Telegram-бота.",
        "Кто сможет настроить Яндекс Директ для интернет-магазина?",
        "Нужен дизайнер, разработать логотип и фирменный стиль.",
        "Нужен монтаж 10 роликов для Reels.",
        "Требуется перевести техническую документацию на русский.",
        "Есть задача по WordPress: нужно исправить форму оплаты.",
        "Кто возьмется сверстать страницу по Figma?",
    )
    assert all(telegram_order_gate(message).passed for message in messages)


def test_telegram_order_gate_drops_spam_and_messages_without_order_signal() -> None:
    messages = (
        "Открыта вакансия Python Backend Developer. Зарплата 200000. График 5/2.",
        "Ищем разработчика в штат. Полная занятость.",
        "Ищу работу Python разработчиком.",
        "Открыт к предложениям, занимаюсь разработкой сайтов.",
        "Предлагаю свои услуги по созданию сайтов.",
        "Делаю Telegram-ботов под ключ. Пишите в личку.",
        "Бесплатный вебинар по продвижению бизнеса.",
        "Подборка вакансий для дизайнеров за неделю.",
        "Telegram выпустил новое обновление.",
        "Python, Django, FastAPI. Пишите в ЛС.",
    )
    assert all(not telegram_order_gate(message).passed for message in messages)


def test_local_filtering_examples_for_telegram_orders() -> None:
    profile = default_search_profile()
    cases = (
        ("Нужно написать Telegram-бота, бюджет 30 000", OrderClassification.MATCHED, "заказ и подходящее направление"),
        ("Ищем Python-разработчика в штат, 5/2", OrderClassification.REJECTED, "признаки вакансии"),
        ("Сделаю Telegram-бота недорого, портфолио покажу в ЛС", OrderClassification.REJECTED, "реклама исполнителя"),
        ("Нужно собирать цены с 20 сайтов ежедневно", OrderClassification.MATCHED, "заказ и подходящее направление"),
        ("Нужно интегрировать amoCRM с сайтом", OrderClassification.MATCHED, "заказ и подходящее направление"),
        ("Требуется доработать WordPress сайт", OrderClassification.MATCHED, "заказ и подходящее направление"),
        ("Кто может помочь с API? Детали в ЛС", OrderClassification.UNDETERMINED, "недостаточно признаков"),
        ("Сегодня солнечно, всем хорошего дня", OrderClassification.REJECTED, "нет признаков"),
        ("Нужно разработать Python Telegram-бота с парсингом", OrderClassification.MATCHED, "заказ и подходящее направление"),
        ("Нужно написать парсер без бюджета, ТЗ отправлю в личку", OrderClassification.MATCHED, "заказ и подходящее направление"),
    )
    for number, (text, expected, reason) in enumerate(cases):
        result = classify(Order(str(number), "telegram", str(number), text, "", ""), profile)
        assert result.classification == expected, text
        assert reason in result.reason, text


def test_clear_vacancy_is_rejected_even_when_an_include_keyword_matches() -> None:
    profile = default_search_profile()
    profile.include_keywords = ["python"]
    text = "Вакансия Python Backend. Ищем в команду. Зарплата 200 000 ₽, график 5/2."
    result = classify(Order("vacancy", "telegram", "vacancy", text, "", ""), profile)
    assert result.classification == OrderClassification.REJECTED
    assert result.reason == "признаки вакансии"


def test_project_order_with_looking_for_developer_is_not_vacancy() -> None:
    profile = default_search_profile()
    profile.include_keywords = ["python"]
    text = "Ищу Python-разработчика. Нужно сделать Telegram-бота. Бюджет 30 000 ₽."
    result = classify(Order("project", "telegram", "project", text, "", ""), profile)
    assert result.classification == OrderClassification.MATCHED


def test_single_schedule_signal_is_not_double_counted_as_vacancy() -> None:
    profile = default_search_profile()
    profile.include_keywords = ["python"]
    text = "Ищу Python-разработчика для проекта. Нужно сделать бота, бюджет 30 000 ₽, график 5/2."
    result = classify(Order("scheduled-project", "telegram", "scheduled-project", text, "", ""), profile)
    assert result.classification == OrderClassification.MATCHED


def test_user_exclude_has_priority_over_include() -> None:
    profile = default_search_profile()
    profile.include_keywords = ["ручная метка"]
    profile.exclude_keywords = ["python"]
    matched = classify(Order("override", "telegram", "1", "Ручная метка Python", "", ""), profile)
    rejected = classify(Order("exclude", "telegram", "2", "Python Telegram-бот", "", ""), profile)
    assert matched.classification == OrderClassification.REJECTED
    assert rejected.classification == OrderClassification.REJECTED


def test_user_exclude_vetoes_a_message_that_passed_telegram_order_gate() -> None:
    database = Database(":memory:")
    profile = default_search_profile()
    profile.exclude_keywords = ["не брать"]
    service = OrderService(database, profile)
    result = service.intake_telegram_message(TelegramMessageEnvelope(
        "-1008", "2", "Нужно сделать Telegram-бота, не брать.",
    ))
    assert result.order is not None
    assert result.order.classification == OrderClassification.REJECTED
    assert result.matched is False
    database.close()


def test_search_profile_and_order_classification_are_persisted() -> None:
    database = Database(":memory:")
    profile = SearchProfile("default", "Все заказы", ["бот"], ["вакансия"])
    database.save_search_profile(profile)
    database.save_order(Order("classified", "telegram", "1", "Вакансия", "", "", classification=OrderClassification.REJECTED))
    assert database.load_search_profile() == profile
    assert database.get_order("classified").classification == OrderClassification.REJECTED
    database.close()


def test_extended_search_profile_rules_and_categories_survive_restart() -> None:
    database = Database(":memory:")
    profile = default_search_profile()
    profile.include_keywords = ["срочно"]
    profile.service_categories[0].enabled = False
    profile.service_categories[1].important_rules = ["сложный бот"]
    database.save_search_profile(profile)
    loaded = database.load_search_profile()
    assert loaded.include_keywords == ["срочно"]
    assert loaded.order_signals == profile.order_signals
    assert loaded.service_categories[0].enabled is False
    assert loaded.service_categories[1].important_rules == ["сложный бот"]
    database.close()


def test_intake_saves_undetermined_order_without_notifying_match() -> None:
    database = Database(":memory:")
    service = OrderService(database, SearchProfile("p", "p", ["python"], ["вакансия"]))
    payload = {"chat": {"id": -1003}, "message": {"message_id": 9}, "text": "Нужен дизайнер"}
    result = service.intake_telegram_post(payload)
    assert result.is_new is True
    assert result.matched is False
    assert database.get_order(result.order.id).classification == OrderClassification.UNDETERMINED
    database.close()


def test_gate_drops_messages_without_order_signal_before_sqlite() -> None:
    database = Database(":memory:")
    service = OrderService(database, default_search_profile())
    result = service.intake_telegram_message(TelegramMessageEnvelope(
        "-1005", "1", "Python, Django, FastAPI. Пишите в ЛС.",
    ))
    assert result.order is None
    assert result.drop_reason == "no_order_signal"
    assert database.count("orders") == 0
    assert any(row["action"] == "telegram_order_gate" and row["details"] == "no_order_signal"
               for row in database.list_logs())
    database.close()


def test_gate_drops_hard_stop_before_sqlite_and_delivery() -> None:
    database = Database(":memory:")
    app, sent = _filter_test_app(database, default_search_profile())
    result = app._on_telegram_user_message(TelegramMessageEnvelope(
        "-100901", "99", "Вакансия Python Backend. Ищем в команду. Зарплата 200 000 ₽, график 5/2.",
    ))
    assert result.order is None
    assert result.drop_reason == "hard_stop:vacancy"
    assert database.count("orders") == 0
    assert sent == []
    database.close()


def test_account_manager_caps_active_accounts() -> None:
    manager = AccountManager(2)
    for i in range(5):
        manager.add(Account(str(i), "kwork", str(i), AccountKind.MARKETPLACE))
    assert len(manager.all()) == 5
    assert len(manager.active()) == 2


def test_memory_guard_limit_is_configurable() -> None:
    guard = MemoryGuard(4096, 5120)
    assert guard.soft_limit_mb == 4096
    assert guard.hard_limit_mb == 5120


def test_telegram_order_is_saved_once() -> None:
    database = Database(":memory:")
    service = OrderService(database)
    payload = {"chat": {"id": -1001, "username": "orders"}, "message": {"message_id": 7}, "text": "Нужен Python разработчик\nСделать бота"}
    first = service.intake_telegram_post(payload)
    second = service.intake_telegram_post(payload)
    assert first.is_new is True
    assert second.is_new is False
    assert database.count("orders") == 1
    database.close()


def test_bot_and_user_and_realtime_history_share_one_telegram_order() -> None:
    database = Database(":memory:")
    service = OrderService(database)
    payload = {"chat": {"id": -10042, "type": "channel"}, "message": {"message_id": 77}, "text": "Нужно сделать Telegram-бота"}
    bot = service.intake_telegram_post(payload)
    user = service.intake_telegram_message(TelegramMessageEnvelope("-10042", "77", "Нужно сделать Telegram-бота"))
    historical = service.intake_telegram_message(TelegramMessageEnvelope("-10042", "77", "Нужно сделать Telegram-бота"))
    assert bot.is_new is True
    assert user.is_new is historical.is_new is False
    assert database.count("orders") == 1
    assert database.connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='orders'").fetchone()[0].find("UNIQUE(platform_id, external_id)") >= 0
    database.close()


def test_concurrent_telegram_order_insert_is_unique_and_reports_one_new() -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from uuid import uuid4

    path = __import__("pathlib").Path("data") / f"parallel-{uuid4().hex}.sqlite3"
    databases = [Database(str(path)), Database(str(path))]
    services = [OrderService(item) for item in databases]
    barrier = Barrier(2)

    def intake(service):
        barrier.wait()
        return service.intake_telegram_message(TelegramMessageEnvelope("-10077", "11", "Нужно сделать Telegram-бота")).is_new

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(intake, services))
        assert sorted(results) == [False, True]
        assert databases[0].count("orders") == 1
    finally:
        for database in databases:
            database.close()
        path.unlink(missing_ok=True)
        path.with_name(path.name + "-journal").unlink(missing_ok=True)


def test_automatic_order_delivery_is_durable_and_not_repeated_after_restart() -> None:
    from pathlib import Path
    from uuid import uuid4

    path = Path("data") / f"delivery-{uuid4().hex}.sqlite3"
    database = Database(str(path))
    service = OrderService(database, SearchProfile("p", "p", ["python"], []))
    app = Application.__new__(Application)
    app.database = database
    app.orders = service
    app.notifications_enabled = True
    app.telegram_feed = TelegramOrderFeed(database)
    sent = []
    app.telegram_transport = SimpleNamespace(
        allowed_user_ids={7},
        send_message_result=lambda *_args: sent.append(True) or {"ok": True, "result": {"message_id": 991}},
    )
    order = service.intake_telegram_message(TelegramMessageEnvelope("-10081", "12", "Нужно сделать Python-бота")).order
    app._notify_new_order(order)
    _wait_telegram_work(app)
    database.close()

    reopened = Database(str(path))
    app.database = reopened
    app.telegram_feed = TelegramOrderFeed(reopened)
    app._notify_new_order(reopened.get_order(order.id))
    _wait_telegram_work(app)
    delivery = reopened.telegram_order_delivery(order.id, 7)
    assert len(sent) == 1
    assert delivery["status"] == "sent"
    assert delivery["telegram_message_id"] == "991"
    reopened.close()
    path.unlink(missing_ok=True)
    path.with_name(path.name + "-journal").unlink(missing_ok=True)


def test_failed_telegram_order_delivery_is_not_marked_sent_and_can_retry() -> None:
    database = Database(":memory:")
    service = OrderService(database, SearchProfile("p", "p", ["python"], []))
    app = Application.__new__(Application)
    app.database = database
    app.orders = service
    app.notifications_enabled = True
    app.telegram_feed = TelegramOrderFeed(database)
    attempts = iter([{"ok": False}, {"ok": True, "result": {"message_id": 4}}])
    app.telegram_transport = SimpleNamespace(allowed_user_ids={7}, send_message_result=lambda *_: next(attempts))
    order = service.intake_telegram_message(TelegramMessageEnvelope("-10082", "13", "Нужно сделать Python-бота")).order
    app._notify_new_order(order)
    _wait_telegram_work(app)
    assert database.telegram_order_delivery(order.id, 7) is None
    app._notify_new_order(order)
    _wait_telegram_work(app)
    assert database.telegram_order_delivery(order.id, 7)["status"] == "sent"
    database.close()


def _filter_test_app(database, profile):
    """Build an offline Telegram application with an observable owner delivery."""
    app = _telegram_app(database, [TelegramSource("-100901")])
    app.orders = OrderService(database, profile)
    sent = []
    app.telegram_transport.allowed_user_ids = {7}
    app.telegram_transport.send_message = lambda _chat, text, _markup=None: sent.append(text) or True
    return app, sent


def test_matched_telegram_order_is_saved_classified_and_delivered() -> None:
    database = Database(":memory:")
    app, sent = _filter_test_app(database, default_search_profile())
    assert app._on_telegram_post(_telegram_payload(
        901, chat_id="-100901",
        text="Ищу Python-разработчика. Нужно сделать Telegram-бота. Бюджет 30 000 ₽.",
    ))
    _wait_telegram_work(app)
    order = database.list_orders(1)[0]
    assert order.classification == OrderClassification.MATCHED
    assert len(sent) == 1
    assert database.telegram_order_delivery(order.id, 7)["status"] == "sent"
    database.close()


def test_excluded_telegram_order_is_persisted_but_never_delivered() -> None:
    database = Database(":memory:")
    profile = default_search_profile()
    profile.exclude_keywords = ["не брать"]
    app, sent = _filter_test_app(database, profile)
    assert app._on_telegram_post(_telegram_payload(902, chat_id="-100901", text="Нужно написать Python-бота, не брать"))
    order = database.list_orders(1)[0]
    assert order.classification == OrderClassification.REJECTED
    assert sent == []
    assert database.telegram_order_delivery(order.id, 7) is None
    database.close()


def test_explicit_vacancy_is_dropped_before_creating_an_order() -> None:
    database = Database(":memory:")
    profile = default_search_profile()
    profile.include_keywords = ["python"]
    app, sent = _filter_test_app(database, profile)
    result = app._on_telegram_user_message(TelegramMessageEnvelope(
        "-100901", "9020",
        "Вакансия Python Backend. Ищем в команду. Зарплата 200 000 ₽, график 5/2.",
    ))
    assert result.order is None
    assert result.drop_reason == "hard_stop:vacancy"
    assert result.matched is False
    assert sent == []
    assert database.count("orders") == 0
    database.close()


def test_message_without_order_signal_is_dropped_before_creating_an_order() -> None:
    database = Database(":memory:")
    app, sent = _filter_test_app(database, default_search_profile())
    result = app._on_telegram_user_message(TelegramMessageEnvelope(
        "-100901", "9021", "Кто может помочь с API? Детали в ЛС",
    ))
    assert result.order is None
    assert result.drop_reason == "no_order_signal"
    assert result.matched is False
    assert sent == []
    assert database.count("orders") == 0
    database.close()


def test_bot_and_user_api_apply_the_same_filter_result_and_delivery_gate() -> None:
    database = Database(":memory:")
    profile = default_search_profile()
    profile.exclude_keywords = ["не брать"]
    app, sent = _filter_test_app(database, profile)
    payload = _telegram_payload(903, chat_id="-100901", message_id=903, text="Нужно написать Python-бота, не брать")
    assert app._on_telegram_post(payload)
    bot_saved = database.list_orders(1)[0]
    user_result = app._on_telegram_user_message(TelegramMessageEnvelope("-100901", "903", payload["text"]))
    assert bot_saved.classification == user_result.order.classification == OrderClassification.REJECTED
    assert user_result.matched is False
    assert sent == []
    database.close()


def test_repeated_historical_sync_of_rejected_order_never_delivers() -> None:
    database = Database(":memory:")
    profile = default_search_profile()
    profile.exclude_keywords = ["не брать"]
    app, sent = _filter_test_app(database, profile)
    first = _telegram_payload(904, chat_id="-100901", message_id=904, text="Нужно написать Python-бота, не брать")
    second = _telegram_payload(905, chat_id="-100901", message_id=904, text=first["text"])
    assert app._on_telegram_post(first)
    assert app._on_telegram_post(second)
    app.process_pending_telegram_sources(datetime.now(timezone.utc) + timedelta(minutes=3))
    _wait_telegram_work(app)
    assert database.count("orders") == 1
    assert database.pending_telegram_updates("-100901") == []
    assert database.list_orders(1)[0].classification == OrderClassification.REJECTED
    assert sent == []
    database.close()


def test_restarting_does_not_turn_rejected_order_into_deliverable() -> None:
    from pathlib import Path
    from uuid import uuid4

    path = Path("data") / f"rejected-{uuid4().hex}.sqlite3"
    database = Database(str(path))
    profile = default_search_profile()
    profile.exclude_keywords = ["не брать"]
    database.save_search_profile(profile)
    app, sent = _filter_test_app(database, profile)
    payload = _telegram_payload(906, chat_id="-100901", message_id=906, text="Нужно написать Python-бота, не брать")
    assert app._on_telegram_post(payload)
    rejected = database.list_orders(1)[0]
    database.close()

    reopened = Database(str(path))
    reloaded_profile = reopened.load_search_profile()
    restarted, restarted_sent = _filter_test_app(reopened, reloaded_profile)
    assert restarted._on_telegram_user_message(TelegramMessageEnvelope("-100901", "906", payload["text"]))
    assert reopened.get_order(rejected.id).classification == OrderClassification.REJECTED
    assert sent == restarted_sent == []
    assert reopened.telegram_order_delivery(rejected.id, 7) is None
    reopened.close()
    path.unlink(missing_ok=True)
    path.with_name(path.name + "-journal").unlink(missing_ok=True)


def test_delivery_gate_rechecks_persisted_classification_before_sending() -> None:
    database = Database(":memory:")
    app, sent = _filter_test_app(database, default_search_profile())
    order = OrderService(database, default_search_profile()).intake_telegram_message(
        TelegramMessageEnvelope("-100901", "907", "Нужно написать Telegram-бота для Python")
    ).order
    database.connection.execute(
        "UPDATE orders SET classification=? WHERE id=?",
        (OrderClassification.REJECTED.value, order.id),
    )
    database.connection.commit()
    app._notify_new_order(order)
    _wait_telegram_work(app)
    assert sent == []
    assert database.telegram_order_delivery(order.id, 7) is None
    database.close()


def test_duplicate_repost_from_another_channel_reuses_order_and_delivery() -> None:
    database = Database(":memory:")
    app, sent = _filter_test_app(database, default_search_profile())
    text = "Нужно сделать Telegram-бота для Python. Бюджет 30 000 ₽."
    assert app._on_telegram_post(_telegram_payload(908, chat_id="-100901", message_id=10, text=text))
    _wait_telegram_work(app)
    first_order = database.list_orders(1)[0]
    repeated = app._on_telegram_user_message(TelegramMessageEnvelope(
        "-100902", "55", f"  {text.upper()}\nhttps://t.me/other_channel/55  ",
    ))
    assert repeated.is_new is False
    assert repeated.order.id == first_order.id
    assert database.count("orders") == 1
    assert database.connection.execute("SELECT COUNT(*) FROM telegram_message_aliases").fetchone()[0] == 2
    assert database.connection.execute("SELECT COUNT(*) FROM telegram_order_deliveries").fetchone()[0] == 1
    assert len(sent) == 1
    database.close()


def test_similar_but_different_telegram_orders_keep_separate_fingerprints() -> None:
    database = Database(":memory:")
    profile = default_search_profile()
    profile.include_keywords = ["telegram"]
    app, sent = _filter_test_app(database, profile)
    first = app._on_telegram_user_message(TelegramMessageEnvelope(
        "-100901", "61", "Нужно сделать Telegram-бот для уведомлений.",
    ))
    second = app._on_telegram_user_message(TelegramMessageEnvelope(
        "-100902", "62", "Нужно сделать Telegram-бот для интернет-магазина.",
    ))
    _wait_telegram_work(app)
    assert first.is_new is second.is_new is True
    assert first.order.id != second.order.id
    assert first.order.content_fingerprint != second.order.content_fingerprint
    assert database.count("orders") == 2
    assert len(sent) == 2
    database.close()


def test_fingerprint_preserves_distinct_customer_contact_links() -> None:
    first = OrderService.telegram_content_fingerprint("Нужен Telegram-бот. Контакт: https://t.me/client_one")
    second = OrderService.telegram_content_fingerprint("Нужен Telegram-бот. Контакт: https://t.me/client_two")
    assert first != second
    body_link = OrderService.telegram_content_fingerprint("Заказ с ссылкой https://t.me/source_one/12 внутри описания")
    other_body_link = OrderService.telegram_content_fingerprint("Заказ с ссылкой https://t.me/source_two/12 внутри описания")
    assert body_link != other_body_link


def test_repost_dedup_and_delivery_persist_after_restart() -> None:
    from pathlib import Path
    from uuid import uuid4

    path = Path("data") / f"telegram-reposts-{uuid4().hex}.sqlite3"
    database = Database(str(path))
    profile = default_search_profile()
    database.save_search_profile(profile)
    app, sent = _filter_test_app(database, profile)
    text = "Нужно сделать Telegram-бота для Python. Бюджет 30 000 ₽."
    app._on_telegram_user_message(TelegramMessageEnvelope("-100901", "71", text))
    _wait_telegram_work(app)
    canonical = database.list_orders(1)[0]
    fingerprint = canonical.content_fingerprint
    database.close()

    reopened = Database(str(path))
    restarted, restart_sent = _filter_test_app(reopened, reopened.load_search_profile())
    result = restarted._on_telegram_user_message(TelegramMessageEnvelope(
        "-100903", "88", f"{text}\nhttps://t.me/repost/88",
    ))
    assert result.is_new is False
    assert result.order.id == canonical.id
    assert reopened.count("orders") == 1
    assert reopened.get_order(canonical.id).content_fingerprint == fingerprint
    assert reopened.telegram_order_delivery(canonical.id, 7)["status"] == "sent"
    assert sent and restart_sent == []
    reopened.close()
    path.unlink(missing_ok=True)
    path.with_name(path.name + "-journal").unlink(missing_ok=True)


def test_telegram_order_keeps_feed_metadata_and_budget() -> None:
    database = Database(":memory:")
    service = OrderService(database)
    payload = {
        "chat": {"id": -1002, "username": "jobs", "title": "Работа"},
        "message": {"message_id": 8, "date": 1_700_000_000, "from": {"first_name": "Иван", "username": "ivan"}},
        "text": "Нужен разработчик\nБюджет 25 000 ₽",
    }
    result = service.intake_telegram_post(payload)
    assert result.order.budget == 25000
    assert result.order.currency == "RUB"
    assert result.order.metadata["author_name"] == "Иван"
    assert result.order.metadata["source_name"] == "Работа"
    database.close()


def _inline_buttons(reply):
    return [button for row in (reply.reply_markup or {}).get("inline_keyboard", []) for button in row]


def _recent_order(number, *, classification=OrderClassification.MATCHED, age_hours=1):
    published = datetime.now(timezone.utc) - timedelta(hours=age_hours, minutes=number)
    return Order(
        f"order-{number}", "telegram", f"public_jobs:{number}", f"Заголовок {number}", "", f"https://t.me/public_jobs/{number}",
        classification=classification, published_at=published,
        metadata={"source_name": "Python Freelance", "original_text": f"Оригинальное объявление {number}\n#python"},
    )


def test_telegram_feed_uses_original_text_author_and_distinct_verified_links() -> None:
    database = Database(":memory:")
    service = OrderService(database, SearchProfile("p", "p", ["python"], []))
    original = "Нужен Python-разработчик\n\nСделать Telegram-бота.\nСрок: три дня."
    result = service.intake_telegram_post({
        "chat": {"id": -1002, "type": "supergroup", "username": "public_jobs", "title": "Работа"},
        "message": {"message_id": 8, "date": int(datetime.now(timezone.utc).timestamp()), "from": {"id": 55, "first_name": "Иван", "username": "ivan_dev"}},
        "text": original,
    })
    reply = TelegramOrderFeed(database).command(7, "")[0]
    assert original in reply.text
    assert "Автор: Иван (@ivan_dev)" in reply.text
    buttons = _inline_buttons(reply)
    assert next(button for button in buttons if button["text"] == "✉️ Написать")["url"] == "https://t.me/ivan_dev"
    assert next(button for button in buttons if button["text"] == "🔗 Оригинал")["url"] == "https://t.me/public_jobs/8"
    assert result.order.metadata["original_text"] == original
    database.close()


def test_telegram_feed_shows_source_publication_time_after_original_text() -> None:
    database = Database(":memory:")
    service = OrderService(database, SearchProfile("p", "p", ["python"], []))
    published = datetime.now(timezone.utc) - timedelta(minutes=17)
    original = "Ищу Python-разработчика.\n\nНужно написать парсер."
    service.intake_telegram_message(TelegramMessageEnvelope(
        "-1002", "8", original, "Работа", "channel", "public_jobs", published,
    ))
    reply = TelegramOrderFeed(database).command(7, "")[0]
    label = published.astimezone().strftime("%d.%m.%Y, %H:%M")
    assert original in reply.text
    assert f"\n\nОпубликовано: {label}" in reply.text
    assert reply.text.index(original) < reply.text.index("Опубликовано:")
    database.close()


def test_telegram_feed_does_not_replace_author_with_channel_link() -> None:
    database = Database(":memory:")
    service = OrderService(database)
    service.intake_telegram_post({
        "chat": {"id": -100123, "type": "supergroup", "title": "Закрытая группа"},
        "message": {"message_id": 3, "date": int(datetime.now(timezone.utc).timestamp()), "sender_chat": {"id": -100123, "title": "Закрытая группа"}},
        "text": "Нужно сделать Telegram-бота. Оригинальный текст",
    })
    labels = {button["text"] for button in _inline_buttons(TelegramOrderFeed(database).command(7, "неопределено")[0])}
    assert "✉️ Написать" not in labels
    assert "🔗 Оригинал" in labels
    database.close()


def test_public_telegram_channel_persists_direct_post_link_and_author() -> None:
    database = Database(":memory:")
    order = OrderService(database).intake_telegram_post({
        "chat": {"id": -100101, "type": "channel", "username": "python_jobs", "title": "Python jobs"},
        "message": {"message_id": 14, "from": {"id": 42, "username": "client_dev"}},
        "text": "Нужен разработчик",
    }).order
    saved = database.get_order(order.id)
    assert saved.url == "https://t.me/python_jobs/14"
    assert saved.metadata["chat_id"] == "-100101"
    assert saved.metadata["message_id"] == "14"
    assert saved.metadata["source_username"] == "python_jobs"
    assert saved.metadata["author_contact_url"] == "https://t.me/client_dev"
    labels = {button["text"] for button in _inline_buttons(TelegramOrderFeed(database).command(7, "неопределено")[0])}
    assert {"✉️ Написать", "🔗 Оригинал", "🏠 Меню"} <= labels
    buttons = _inline_buttons(TelegramOrderFeed(database).command(7, "неопределено")[0])
    assert next(button["url"] for button in buttons if button["text"] == "✉️ Написать") == "https://t.me/client_dev"
    database.close()


def test_bot_api_author_without_username_uses_safe_user_id_deep_link() -> None:
    database = Database(":memory:")
    order = OrderService(database).intake_telegram_post({
        "chat": {"id": -100101, "type": "channel", "username": "python_jobs"},
        "message": {"message_id": 140, "from": {"id": 4200, "first_name": "Иван"}},
        "text": "Нужно сделать Telegram-бота",
    }).order
    saved = database.get_order(order.id)
    buttons = _inline_buttons(TelegramOrderFeed(database).command(7, "неопределено")[0])
    assert saved.metadata["author_entity_type"] == "user"
    assert saved.metadata["author_contact_url"] == "tg://user?id=4200"
    assert next(button["url"] for button in buttons if button["text"] == "✉️ Написать") == "tg://user?id=4200"
    assert next(button["url"] for button in buttons if button["text"] == "🔗 Оригинал") == "https://t.me/python_jobs/140"
    database.close()


def test_bot_api_sender_chat_overrides_fake_sender_user_contact() -> None:
    database = Database(":memory:")
    order = OrderService(database).intake_telegram_post({
        "chat": {"id": -100102, "type": "supergroup", "username": "freelance_chat"},
        "message": {
            "message_id": 141,
            "from": {"id": 1087968824, "is_bot": True, "username": "GroupAnonymousBot"},
            "sender_chat": {"id": -100102, "title": "Freelance"},
        },
        "text": "Нужно сделать Telegram-бота",
    }).order
    saved = database.get_order(order.id)
    buttons = _inline_buttons(TelegramOrderFeed(database).command(7, "неопределено")[0])
    assert saved.metadata["author_entity_type"] == "sender_chat"
    assert not any(button["text"] == "✉️ Написать" for button in buttons)
    assert any(button["text"] == "🔗 Оригинал" for button in buttons)
    database.close()


def test_channel_post_without_user_has_no_author_contact_but_keeps_original() -> None:
    database = Database(":memory:")
    order = OrderService(database).intake_telegram_post({
        "chat": {"id": -100103, "type": "channel", "username": "orders_channel"},
        "message": {"message_id": 142},
        "text": "Нужно сделать сайт",
    }).order
    buttons = _inline_buttons(TelegramOrderFeed(database).command(7, "неопределено")[0])
    assert database.get_order(order.id).metadata["author_entity_type"] == "channel"
    assert not any(button["text"] == "✉️ Написать" for button in buttons)
    assert next(button["url"] for button in buttons if button["text"] == "🔗 Оригинал") == "https://t.me/orders_channel/142"
    database.close()


def test_author_contact_comes_from_message_metadata_not_body_username() -> None:
    descriptor, path = tempfile.mkstemp(prefix="contact-persistence-", suffix=".sqlite3", dir=".")
    os.close(descriptor)
    try:
        database = Database(path)
        order = OrderService(database).intake_telegram_post({
            "chat": {"id": -100104, "type": "channel", "username": "orders_channel"},
            "message": {"message_id": 143, "from": {"id": 4300, "username": "real_author"}},
            "text": "Нужно сделать сайт. Упоминаю @someone_else в описании.",
        }).order
        database.close()

        reopened = Database(path)
        saved = reopened.get_order(order.id)
        assert saved.metadata["author_contact_url"] == "https://t.me/real_author"
        assert saved.metadata["declared_contact_url"] == ""
        feed = TelegramOrderFeed(reopened)
        buttons = _inline_buttons(feed.command(7, "неопределено")[0])
        assert next(button["url"] for button in buttons if button["text"] == "✉️ Написать") == "https://t.me/real_author"
        assert next(button["url"] for button in buttons if button["text"] == "🔗 Оригинал") == "https://t.me/orders_channel/143"
        reopened.close()
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(path + suffix)
            except FileNotFoundError:
                pass


def test_public_telegram_group_uses_its_specific_post_link() -> None:
    database = Database(":memory:")
    order = OrderService(database).intake_telegram_post({
        "chat": {"id": -100102, "type": "supergroup", "username": "freelance_chat", "title": "Фриланс"},
        "message": {"message_id": 15}, "text": "Нужен дизайнер",
    }).order
    reply = TelegramOrderFeed(database).command(7, "неопределено")[0]
    original = next(button for button in _inline_buttons(reply) if button["text"] == "🔗 Оригинал")
    assert original["url"] == "https://t.me/freelance_chat/15"
    database.close()


def test_private_supergroup_uses_documented_private_post_link() -> None:
    database = Database(":memory:")
    order = OrderService(database).intake_telegram_post({
        "chat": {"id": -1001234567890, "type": "supergroup", "title": "Закрытая группа"},
        "message": {"message_id": 16}, "text": "Нужен разработчик",
    }).order
    reply = TelegramOrderFeed(database).command(7, "неопределено")[0]
    original = next(button for button in _inline_buttons(reply) if button["text"] == "🔗 Оригинал")
    assert order.url == "https://t.me/c/1234567890/16"
    assert original["url"] == order.url
    database.close()


def test_explicit_contact_is_distinct_from_an_unrelated_username() -> None:
    database = Database(":memory:")
    service = OrderService(database)
    with_contact = service.intake_telegram_post({
        "chat": {"id": -100103, "type": "channel", "username": "orders"},
        "message": {"message_id": 17},
        "text": "Нужно сделать сайт. Хэштег #web\nКонтакт для связи: @real_client",
    }).order
    without_contact = service.intake_telegram_post({
        "chat": {"id": -100103, "type": "channel", "username": "orders"},
        "message": {"message_id": 18},
        "text": "Нужно сделать сайт, обсуждаем @someone без указания способа связи.",
    }).order
    assert with_contact.metadata["declared_contact_url"] == "https://t.me/real_client"
    assert without_contact.metadata["declared_contact_url"] == ""
    replies = TelegramOrderFeed(database).command(7, "неопределено")
    declared_reply = next(reply for reply in replies if "Контакт для связи" in reply.text)
    assert "✉️ Написать" in {button["text"] for button in _inline_buttons(declared_reply)}
    database.close()


def test_anonymous_author_and_non_linkable_private_group_have_no_false_buttons() -> None:
    database = Database(":memory:")
    OrderService(database).intake_telegram_post({
        "chat": {"id": -12345, "type": "group", "title": "Частная группа"},
        "message": {"message_id": 19, "sender_chat": {"id": -12345, "title": "Частная группа"}},
        "text": "Нужно сделать работу без контакта для связи.",
    })
    labels = {button["text"] for button in _inline_buttons(TelegramOrderFeed(database).command(7, "неопределено")[0])}
    assert "✉️ Написать" not in labels
    assert "🔗 Оригинал" not in labels
    assert "🏠 Меню" in labels
    database.close()


def test_retained_telegram_inbox_backfills_old_order_metadata() -> None:
    database = Database(":memory:")
    old = Order(
        "old", "telegram", "-100104:20", "Старое", "", "", metadata={"chat_id": "-100104", "message_id": "20"},
    )
    database.save_order(old)
    payload = {
        "chat": {"id": -100104, "type": "channel", "username": "old_jobs", "title": "Старые заказы"},
        "message": {"message_id": 20, "from": {"id": 99, "username": "old_client"}},
        "text": "Пишите @old_client для связи",
    }
    database.enqueue_telegram_update(20, payload)
    assert OrderService(database).recover_telegram_metadata() == 1
    restored = database.get_order("old")
    assert restored.url == "https://t.me/old_jobs/20"
    assert restored.metadata["author_user_id"] == 99
    labels = {button["text"] for button in _inline_buttons(TelegramOrderFeed(database).command(7, "")[0])}
    assert {"✉️ Написать", "🔗 Оригинал", "🏠 Меню"} <= labels
    database.close()


def test_orders_sends_first_ten_separate_recent_messages() -> None:
    database = Database(":memory:")
    for number in range(12):
        database.save_order(_recent_order(number))
    feed = TelegramOrderFeed(database, lambda user_id: user_id == 7)
    transport = TelegramPolling("token", {7})
    sent = []
    transport.send_message = lambda _chat_id, text, reply_markup=None: sent.append(TelegramReply(text, reply_markup)) or True
    transport.command("orders", feed.command)
    assert transport._handle_update({"message": {"chat": {"id": 7}, "from": {"id": 7}, "text": "/orders"}})
    assert len(sent) == 10
    assert all("Оригинальное объявление" in reply.text for reply in sent)
    assert any(button["text"] == "Показать ещё" for button in _inline_buttons(sent[-1]))
    database.close()


def test_show_more_loads_next_page_without_repeating_orders() -> None:
    database = Database(":memory:")
    for number in range(15):
        database.save_order(_recent_order(number))
    feed = TelegramOrderFeed(database, lambda _user: True)
    first = feed.command(7, "")
    more = next(button for button in _inline_buttons(first[-1]) if button["text"] == "Показать ещё")
    result = feed.callback(7, more["callback_data"])
    second = result.messages
    assert result.edit is not None
    first_texts = {reply.text for reply in first}
    assert len(second) == 5
    assert not first_texts.intersection(reply.text for reply in second)
    database.close()


def test_main_feed_excludes_orders_older_than_48_hours() -> None:
    database = Database(":memory:")
    database.save_order(_recent_order(1, age_hours=1))
    database.save_order(_recent_order(2, age_hours=49))
    replies = TelegramOrderFeed(database).command(7, "")
    assert "объявление 1" in replies[0].text
    assert "объявление 2" not in replies[0].text
    database.close()


def test_main_telegram_feed_is_strictly_sorted_by_source_publication_time() -> None:
    database = Database(":memory:")
    older = _recent_order(1, age_hours=2)
    newer = _recent_order(2, age_hours=1)
    # Discovery/check time must not make an older source post jump ahead.
    older.discovered_at = datetime.now(timezone.utc)
    database.save_order(older)
    database.save_order(newer)
    replies = TelegramOrderFeed(database).command(7, "")
    assert "Оригинальное объявление 2" in replies[0].text
    assert "Оригинальное объявление 1" in replies[1].text
    database.close()


def test_undetermined_feed_keeps_older_saved_orders() -> None:
    database = Database(":memory:")
    database.save_order(_recent_order(1, classification=OrderClassification.UNDETERMINED, age_hours=72))
    replies = TelegramOrderFeed(database).command(7, "неопределено")
    assert "Оригинальное объявление 1" in replies[0].text
    database.close()


def test_long_text_opens_safe_continuation_without_losing_newlines() -> None:
    database = Database(":memory:")
    order = _recent_order(1)
    order.metadata["original_text"] = "Первая строка\nВторая строка\n" + ("Текст\n" * 1000)
    database.save_order(order)
    feed = TelegramOrderFeed(database)
    card = feed.command(7, "")[0]
    assert len(card.text) <= 4096
    assert "Первая строка\nВторая строка" in card.text
    assert [button["text"] for button in _inline_buttons(card)] == ["🔗 Оригинал", "🏠 Меню"]
    database.close()


def test_telegram_archive_callback_cannot_change_order_status() -> None:
    database = Database(":memory:")
    database.save_order(_recent_order(1))
    feed = TelegramOrderFeed(database, lambda _user_id: True)
    result = feed.callback(7, "feed:archive:m:order-1")
    assert database.get_order("order-1").status == OrderStatus.ACTIVE
    assert "только в desktop" in result.notice
    database.close()


def test_transport_forwards_group_post_without_command() -> None:
    transport = TelegramPolling("token", {123})
    received = []
    transport.on_post(received.append)
    transport._handle_update({"update_id": 1, "message": {"chat": {"id": -10}, "from": {"id": 999}, "message_id": 2, "text": "Новый заказ"}})
    assert received[0]["text"] == "Новый заказ"


def _telegram_app(database, sources):
    """Build only the composition fields needed to test Telegram intake offline."""
    app = Application.__new__(Application)
    app.database = database
    app.orders = OrderService(database)
    app.telegram = SimpleNamespace(running=True)
    app.telegram_sources = {source.chat_id: source for source in sources}
    app.watched_chat_ids = {source.chat_id for source in sources if source.enabled}
    app.telegram_transport = SimpleNamespace(allowed_user_ids=set(), send_message=lambda *_: True)
    app.is_admin = lambda user_id: user_id == 7
    app.telegram_feed = TelegramOrderFeed(database, app.is_admin)
    app.parsing_state = ProcessState.WAITING
    app.notifications_enabled = True
    return app


def _wait_telegram_work(app):
    """Wait for the shared background sender after a test exercises intake."""
    fill = getattr(app, "_telegram_fill_future", None)
    if fill is not None:
        fill.result(timeout=10)
    sender = getattr(app, "_telegram_delivery_executor", None)
    if sender is not None:
        sender.submit(lambda: None).result(timeout=10)


def _telegram_payload(update_id, chat_id="-1001", message_id=1, text="Нужно сделать Python-бота"):
    return {
        "update_id": update_id,
        "chat": {"id": int(chat_id), "title": "Заказы"},
        "message": {"message_id": message_id},
        "text": text,
    }


def test_enabled_telegram_source_is_stored_and_processed() -> None:
    database = Database(":memory:")
    app = _telegram_app(database, [TelegramSource("-1001", poll_interval_seconds=30)])
    assert app._on_telegram_post(_telegram_payload(10)) is True
    assert database.count("orders") == 1
    assert database.pending_telegram_updates("-1001") == []
    database.close()


def test_disabled_telegram_source_is_acknowledged_without_intake() -> None:
    database = Database(":memory:")
    app = _telegram_app(database, [TelegramSource("-1001", enabled=False)])
    assert app._on_telegram_post(_telegram_payload(11)) is True
    assert database.count("orders") == 0
    assert database.pending_telegram_updates("-1001") == []
    database.close()


def test_telegram_source_interval_defers_processing_without_losing_updates() -> None:
    database = Database(":memory:")
    source = TelegramSource("-1001", poll_interval_seconds=60)
    app = _telegram_app(database, [source])
    started = datetime(2026, 9, 26, tzinfo=timezone.utc)
    database.enqueue_telegram_update(12, _telegram_payload(12, message_id=12))
    app.process_pending_telegram_sources(started)
    database.enqueue_telegram_update(13, _telegram_payload(13, message_id=13))
    app.process_pending_telegram_sources(started + timedelta(seconds=30))
    assert [update_id for update_id, _ in database.pending_telegram_updates("-1001")] == [13]
    app.process_pending_telegram_sources(started + timedelta(seconds=60))
    assert database.pending_telegram_updates("-1001") == []
    # Both payloads carry the same default text, so content dedup keeps one Order.
    assert database.count("orders") == 1
    database.close()


def test_source_identity_scales_and_deletion_uses_stable_source_id() -> None:
    database = Database(":memory:")
    app = Application.__new__(Application)
    app.database = database
    app.telegram_sources = {}
    app.watched_chat_ids = set()
    app.telegram_user = SimpleNamespace(source_removed=lambda _source: None)
    sources = [TelegramSource(f"-100{i}", f"Source {i}", source_id=f"source-{i}") for i in range(100)]
    for source in sources:
        app.add_telegram_source(source)
        database.mark_telegram_user_message_saved(source.chat_id, "1")
    assert len(app.telegram_sources) == 100
    assert len(database.load_telegram_sources()) == 100
    for index in (0, 50, 99):
        app.remove_telegram_source(sources[index].source_id)
    assert len(app.telegram_sources) == 97
    remaining_ids = {source.source_id for source in database.load_telegram_sources()}
    assert {"source-0", "source-50", "source-99"}.isdisjoint(remaining_ids)
    assert "source-49" in remaining_ids and "source-51" in remaining_ids
    assert database.telegram_user_checkpoint("-10049")["last_message_id"] == "1"
    database.close()


def test_source_disable_enable_and_duplicate_chat_preserve_identity_and_orders() -> None:
    database = Database(":memory:")
    app = Application.__new__(Application)
    app.database = database
    app.telegram_sources = {}
    app.watched_chat_ids = set()
    app.telegram_user = SimpleNamespace(source_removed=lambda _source: None)
    source = TelegramSource("-100777", "Jobs", source_id="stable-777")
    app.add_telegram_source(source)
    app.add_telegram_source(replace(source, enabled=False, name="Jobs updated", source_id="new-id-ignored"))
    assert len(app.telegram_sources) == 1
    assert app.telegram_sources["-100777"].source_id == "stable-777"
    assert not app.telegram_sources["-100777"].enabled
    app.add_telegram_source(replace(source, enabled=True))
    assert app.telegram_sources["-100777"].enabled
    order = Order("kept", "telegram", "-100777:1", "Заказ", "", "", metadata={"chat_id": "-100777", "message_id": "1"})
    database.save_order(order)
    app.remove_telegram_source("stable-777")
    assert database.get_order("kept") is not None
    assert database.load_telegram_sources() == []
    database.close()


def test_telegram_source_reference_formats_resolve_to_one_source() -> None:
    """Equivalent public Telegram references must share one resolved chat row."""
    database = Database(":memory:")
    app = Application.__new__(Application)
    app.database = database
    app.telegram_sources = {}
    app.watched_chat_ids = set()
    app.telegram_user = SimpleNamespace(connected=False, configured=False)
    app.telegram_transport = SimpleNamespace(
        get_chat=lambda _reference: {"id": -1004242, "title": "Python Jobs", "username": "python_jobs"}
    )

    first, resolved = app.add_telegram_source_reference("@python_jobs")
    assert resolved is True
    second, resolved = app.add_telegram_source_reference("https://t.me/python_jobs")
    assert resolved is True
    third, resolved = app.add_telegram_source_reference("http://telegram.me/python_jobs/")
    assert resolved is True

    assert first.chat_id == second.chat_id == third.chat_id == "-1004242"
    assert first.source_id == second.source_id == third.source_id
    assert len(app.telegram_sources) == 1
    assert len(database.load_telegram_sources()) == 1
    database.close()


def test_user_api_add_uses_application_pipeline_once_for_new_and_existing() -> None:
    database = Database(":memory:")
    app = Application.__new__(Application)
    app.database = database
    app.telegram_sources = {}
    app.watched_chat_ids = set()
    added = []

    class FakeUserApi:
        connected = True
        configured = True

        def verify_source(self, _reference):
            return TelegramSource("-1005151", "Jobs", True, "ru", 120, False, "@python_jobs", "user", "available")

        def source_added(self, source):
            added.append(source.source_id)

    app.telegram_user = FakeUserApi()
    first, resolved = app.add_telegram_source_reference("@python_jobs")
    second, resolved_again = app.add_telegram_source_reference("https://t.me/python_jobs")

    assert resolved is True and resolved_again is True
    assert first.source_id == second.source_id
    assert len(database.load_telegram_sources()) == 1
    assert len(added) == 1
    checkpoint = database.telegram_user_checkpoint("-1005151")
    assert checkpoint["initial_history_from_at"]
    assert checkpoint["initial_history_until_at"]
    database.close()


def test_user_api_resolve_failure_does_not_create_working_source_or_checkpoint() -> None:
    database = Database(":memory:")
    app = Application.__new__(Application)
    app.database = database
    app.telegram_sources = {}
    app.watched_chat_ids = set()

    class FailingUserApi:
        connected = True
        configured = True

        def verify_source(self, _reference):
            from freelance_bot.telegram_user.adapter import SourceAccessError
            raise SourceAccessError("forbidden", "not available")

        def source_added(self, _source):
            raise AssertionError("unavailable source must not start sync")

    app.telegram_user = FailingUserApi()
    source, resolved = app.add_telegram_source_reference("@python_jobs")

    assert resolved is False
    assert source.access_status == "forbidden"
    assert source.pending_connection is True
    checkpoint = database.telegram_user_checkpoint(source.chat_id)
    assert checkpoint["initial_history_from_at"] == ""
    assert checkpoint["initial_history_until_at"] == ""
    assert database.load_telegram_sources()[0].access_status == "forbidden"
    database.close()


def test_repeated_telegram_update_is_not_saved_twice() -> None:
    database = Database(":memory:")
    app = _telegram_app(database, [TelegramSource("-1001")])
    payload = _telegram_payload(14, message_id=14)
    assert app._on_telegram_post(payload) is True
    assert app._on_telegram_post(payload) is True
    assert database.count("orders") == 1
    assert database.connection.execute("SELECT COUNT(*) FROM telegram_inbox").fetchone()[0] == 1
    database.close()


def test_telegram_checkpoint_survives_transport_restart_after_durable_handler() -> None:
    database = Database(":memory:")
    transport = TelegramPolling(
        "token", checkpoint_load=lambda: database.get_setting("telegram_updates_offset", "0"),
        checkpoint_save=lambda offset: database.set_setting("telegram_updates_offset", str(offset)),
    )
    transport.on_post(lambda _payload: True)
    assert transport.process_update({"update_id": 20, "message": {"chat": {"id": -1}, "message_id": 1, "text": "Заказ"}})
    restarted = TelegramPolling("token", checkpoint_load=lambda: database.get_setting("telegram_updates_offset", "0"))
    assert restarted._offset == 21
    database.close()


def test_processing_error_keeps_update_for_retry_and_recovers() -> None:
    database = Database(":memory:")
    app = _telegram_app(database, [TelegramSource("-1001")])
    database.enqueue_telegram_update(21, _telegram_payload(21, message_id=21))
    healthy_service = app.orders
    app.orders = SimpleNamespace(intake_telegram_post=lambda _payload: (_ for _ in ()).throw(OSError("temporary")))
    app.process_pending_telegram_sources()
    assert [update_id for update_id, _ in database.pending_telegram_updates("-1001")] == [21]
    assert database.telegram_source_states()["-1001"]["last_error_code"] == "processing_failed"
    app.orders = healthy_service
    app.process_pending_telegram_sources()
    assert database.pending_telegram_updates("-1001") == []
    assert database.count("orders") == 1
    assert database.telegram_source_states()["-1001"]["last_error_code"] == ""
    database.close()


def test_notification_failure_does_not_undo_saved_order() -> None:
    database = Database(":memory:")
    app = _telegram_app(database, [TelegramSource("-1001")])
    app.orders = OrderService(database, SearchProfile("p", "p", ["python"], []))
    app.telegram_transport = SimpleNamespace(allowed_user_ids={7}, send_message=lambda *_: False)
    assert app._on_telegram_post(_telegram_payload(22)) is True
    assert database.count("orders") == 1
    assert database.pending_telegram_updates("-1001") == []
    assert any(row["action"] == "order_notification" and row["status"] == "failed" for row in database.list_logs())
    database.close()


def test_new_user_api_match_is_delivered_as_a_full_feed_card() -> None:
    database = Database(":memory:")
    app = Application.__new__(Application)
    app.database = database
    app.orders = OrderService(database, SearchProfile("p", "p", ["python"], []))
    app.notifications_enabled = True
    app.is_admin = lambda user_id: user_id == 7
    app.telegram_feed = TelegramOrderFeed(database, app.is_admin)
    sent = []
    app.telegram_transport = SimpleNamespace(
        allowed_user_ids={7},
        send_message=lambda _chat_id, text, reply_markup=None: sent.append(TelegramReply(text, reply_markup)) or True,
    )
    published = datetime(2026, 9, 29, 10, 17, tzinfo=timezone.utc)
    result = app._on_telegram_user_message(TelegramMessageEnvelope(
        "-1005", "12", "Нужен разработчик Python. Нужно сделать Telegram-бота.\n\nТекст с переносом.", "Python jobs", "channel",
        "python_jobs", published,
    ))
    _wait_telegram_work(app)
    assert result.is_new is True
    assert len(sent) == 1
    assert "Текст с переносом." in sent[0].text
    assert f"Опубликовано: {published.astimezone().strftime('%d.%m.%Y, %H:%M')}" in sent[0].text
    database.close()


def test_owner_commands_control_notifications_without_stopping_intake() -> None:
    database = Database(":memory:")
    app = _telegram_app(database, [TelegramSource("-1001")])
    app.telegram = SimpleNamespace(running=True, configured=True)
    replies = []
    transport = TelegramPolling("token", {7})
    transport.connected = False
    transport.send_message = lambda _chat_id, text, reply_markup=None: replies.append(text) or True
    transport.send_message_result = lambda _chat_id, text, reply_markup=None: (
        replies.append(text) or {"ok": True, "result": {"message_id": len(replies)}}
    )
    transport.command("start", app._start_command)
    transport.command("status", app._status_command)
    transport.command("stop", app._stop_command)
    app.telegram_transport = transport
    assert transport._handle_update({"message": {"chat": {"id": 7}, "from": {"id": 7}, "text": "/stop"}})
    assert app.notifications_enabled is False
    assert app.telegram.running is True
    assert database.get_setting("telegram_notifications_enabled") == "0"
    assert "Сбор заказов на ПК продолжается" in replies[-1]
    assert app._on_telegram_post(_telegram_payload(23, message_id=23)) is True
    assert database.count("orders") == 1
    assert len(replies) == 1
    assert transport._handle_update({"message": {"chat": {"id": 7}, "from": {"id": 7}, "text": "/status"}})
    assert "Источники: 0 / 0" in replies[-1]
    assert transport._handle_update({"message": {"chat": {"id": 7}, "from": {"id": 7}, "text": "/start"}})
    _wait_telegram_work(app)
    assert app.notifications_enabled is True
    assert database.get_setting("telegram_notifications_enabled") == "1"
    database.close()


def test_start_delivers_recent_order_and_restores_main_menu() -> None:
    database = Database(":memory:")
    app = _telegram_app(database, [TelegramSource("-1001")])
    app.telegram = SimpleNamespace(running=True, configured=True)
    database.save_order(_recent_order(1))
    transport = TelegramPolling("token", {7})
    sent = []
    transport.send_message = lambda _chat_id, text, reply_markup=None: sent.append((text, reply_markup)) or True
    transport.send_message_result = lambda _chat_id, text, reply_markup=None: (
        sent.append((text, reply_markup)) or {"ok": True, "result": {"message_id": len(sent)}}
    )
    app.telegram_transport = transport
    transport.send_message = lambda _chat_id, text, reply_markup=None: sent.append((text, reply_markup)) or True
    transport.command("start", app._start_command)
    assert transport._handle_update({"message": {"chat": {"id": 7}, "from": {"id": 7}, "text": "/start"}})
    _wait_telegram_work(app)
    assert app.notifications_enabled is True
    menu_message = next(item for item in sent if item[1] and "keyboard" in item[1])
    card_message = next(item for item in sent if item[1] and "inline_keyboard" in item[1])
    assert {button["text"] for row in menu_message[1]["keyboard"] for button in row} == {
        "📋 Заказы", "📊 Статус", "⏹ Остановить",
    }
    assert "Оригинальное объявление 1" in card_message[0]
    assert len(sent) == 2
    database.close()


def test_non_admin_cannot_change_telegram_sources() -> None:
    database = Database(":memory:")
    app = _telegram_app(database, [TelegramSource("-1001")])
    assert "Только администратор" in app._source_disable_command(8, "-1001")
    assert app.telegram_sources["-1001"].enabled is True
    assert "Только администратор" in app._source_add_command(8, "@public_jobs")
    assert set(app.telegram_sources) == {"-1001"}
    database.close()


def test_pending_source_is_stored_in_shared_sqlite_and_visible_to_bot() -> None:
    database = Database(":memory:")
    app = _telegram_app(database, [])
    app.telegram_transport = SimpleNamespace(get_chat=lambda _value: None)
    source, resolved = app.add_telegram_source_reference("https://t.me/+invite_code")
    assert resolved is False
    assert source.pending_connection is True
    assert database.load_telegram_sources()[0].chat_id == "https://t.me/+invite_code"
    assert "Ожидает подключения" in app._sources_command(7, "").text
    database.close()


def test_bot_document_handler_is_restricted_to_allowed_users() -> None:
    transport = TelegramPolling("token", {7})
    received = []
    transport.on_document(lambda user_id, document, chat: received.append((user_id, document["file_id"], chat["id"])) or [])
    update = {
        "update_id": 1,
        "message": {"chat": {"id": 7}, "from": {"id": 8}, "document": {"file_id": "secret-file"}},
    }
    assert transport._handle_update(update) is True
    assert received == []
    update["message"]["from"]["id"] = 7
    assert transport._handle_update(update) is True
    assert received == [(7, "secret-file", 7)]


def test_non_owner_cannot_run_telegram_commands() -> None:
    database = Database(":memory:")
    app = _telegram_app(database, [TelegramSource("-1001")])
    app.telegram = SimpleNamespace(running=True, configured=True)
    replies = []
    transport = TelegramPolling("token", {7})
    transport.send_message = lambda _chat_id, text: replies.append(text) or True
    transport.command("stop", app._stop_command)
    app.telegram_transport = transport
    assert transport._handle_update({"message": {"chat": {"id": 9}, "from": {"id": 9}, "text": "/stop"}})
    assert app.notifications_enabled is True
    assert replies == []
    database.close()


def test_accounts_and_pending_jobs_survive_store_reload() -> None:
    database = Database(":memory:")
    database.save_account(Account("a1", "kwork", "main"))
    job = DispatchJob("j1", "o1", "a1", ProposalMode.DRAFT)
    database.save_job(job)
    assert database.load_accounts()[0].name == "main"
    restored = database.load_jobs()
    assert restored[0].id == "j1"
    assert restored[0].status == JobStatus.PENDING
    database.close()


def test_database_returns_saved_order() -> None:
    database = Database(":memory:")
    order = Order("order-1", "telegram", "chat:1", "Нужен бот", "Описание", "https://t.me/x/1")
    database.save_order(order)
    restored = database.get_order("order-1")
    assert restored is not None
    assert restored.title == "Нужен бот"
    database.close()


def test_running_job_becomes_unknown_after_restore() -> None:
    database = Database(":memory:")
    job = DispatchJob("j2", "o2", "a2", ProposalMode.AUTO, status=JobStatus.SENDING)
    database.save_job(job)
    assert database.load_jobs()[0].status == JobStatus.UNKNOWN_RESULT
    database.close()


def test_telegram_sources_are_persisted() -> None:
    database = Database(":memory:")
    database.save_telegram_source(TelegramSource("-1001", "Работа", language="ru"))
    source = database.load_telegram_sources()[0]
    assert source.chat_id == "-1001"
    assert source.name == "Работа"
    database.delete_telegram_source("-1001")
    assert database.load_telegram_sources() == []
    database.close()


def test_empty_owner_list_does_not_execute_telegram_command() -> None:
    transport = TelegramPolling("token", set())
    executed = []
    transport.command("pause", lambda _user, _args: executed.append(True) or "ok")
    transport._handle_update({"message": {"chat": {"id": 1}, "from": {"id": 1}, "text": "/pause"}})
    assert executed == []


def test_logs_can_be_read_for_the_ui() -> None:
    database = Database(":memory:")
    database.log("system", "test_action", "ok", "details")
    row = database.list_logs()[0]
    assert row["action"] == "test_action"
    assert row["details"] == "details"
    database.close()


def test_archived_order_without_history_expires_after_seven_days() -> None:
    database = Database(":memory:")
    order = Order("old", "telegram", "old:1", "Старый", "", "")
    database.save_order(order)
    database.archive_order("old")
    database.connection.execute("UPDATE orders SET archived_at=? WHERE id='old'", ((datetime.now(timezone.utc) - timedelta(days=8)).isoformat(),))
    database.connection.commit()
    assert database.cleanup_archived_orders() == 1
    assert database.get_order("old") is None
    tombstone = database.connection.execute("SELECT * FROM order_tombstones").fetchone()
    assert tombstone["external_id"] == "old:1"
    database.close()


def test_draft_waiting_for_confirmation_is_not_auto_dispatched() -> None:
    queue = DispatchQueue()
    job = DispatchJob("draft", "order", "manual", ProposalMode.DRAFT, status=JobStatus.AWAITING_CONFIRMATION)
    queue.enqueue(job)
    assert queue.pending() == []


def test_feed_can_filter_search_sort_and_archive() -> None:
    database = Database(":memory:")
    low = Order("low", "telegram", "1", "Python task", "", "", budget=100)
    high = Order("high", "telegram", "2", "Design task", "", "", budget=200)
    database.save_order(low)
    database.save_order(high)
    assert database.list_orders(query="Python")[0].id == "low"
    assert database.list_orders(sort="budget_desc")[0].id == "high"
    database.archive_order("low")
    assert database.list_orders(status=OrderStatus.ARCHIVED)[0].id == "low"
    database.close()


def test_telegram_service_requires_token_and_owner() -> None:
    service = TelegramService("token", set())
    service.start()
    assert not service.running
    assert "Нужны токен" in service.last_error


def test_ai_prompt_contains_rules_but_not_account_secrets() -> None:
    order = Order("o3", "telegram", "chat:3", "Нужен бот", "Описание заказа", "https://example.test")
    profile = ProposalProfile("p3", "Профиль", skills=["Python"], rules="Не обещай невозможное")
    prompt = PromptBuilder().build(order, profile)
    assert "Не обещай невозможное" in prompt
    assert "Python" in prompt
    assert "token" not in prompt.lower()
