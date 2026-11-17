"""Telegram Bot API command recovery and durable owner delivery tests."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from freelance_bot.app import Application
from freelance_bot.core.models import Order, OrderClassification, OrderStatus
from freelance_bot.core.order_service import OrderService
from freelance_bot.storage.database import Database
from freelance_bot.telegram_bot.feed import TelegramOrderFeed
from freelance_bot.telegram_bot.transport import TelegramPolling


def build_app(database, enabled=True):
    """Compose Telegram control-plane dependencies without external services."""
    app = Application.__new__(Application)
    app.database = database
    app.notifications_enabled = enabled
    app.orders = OrderService(database)
    app.telegram = SimpleNamespace(configured=True, running=True)
    app.telegram_user = SimpleNamespace(configured=True, connected=False, running=True)
    transport = TelegramPolling("test-token", {7})
    sent = []
    menus = []
    transport.send_message = lambda chat, text, markup=None: menus.append((chat, text, markup)) or True
    transport.menu_sent = menus
    transport.send_message_result = lambda chat, text, markup=None: (
        sent.append((chat, text, markup)) or {"ok": True, "result": {"message_id": len(sent)}}
    )
    app.telegram_transport = transport
    app.telegram_feed = TelegramOrderFeed(database, lambda user_id: user_id == 7)
    app.is_admin = lambda user_id: user_id == 7
    app.parsing_state = SimpleNamespace(value="running")
    return app, sent


def save_order(database, number, *, published_hours=1, collected_hours=1,
               classification=OrderClassification.MATCHED, status=OrderStatus.ACTIVE):
    """Save one Telegram order with independently controllable publish and collect times."""
    now = datetime.now(timezone.utc)
    order = Order(
        f"order-{number}", "telegram", f"channel:{number}", f"Order {number}",
        f"Order text {number}", f"https://t.me/channel/{number}",
        classification=classification, status=status,
        published_at=now - timedelta(hours=published_hours),
        discovered_at=now - timedelta(hours=collected_hours),
        metadata={"original_text": f"Order text {number}"},
    )
    database.save_order(order)
    return order


def wait_for_fill(app):
    """Wait for a background fill task after asserting the command returned promptly."""
    future = getattr(app, "_telegram_fill_future", None)
    return future.result(timeout=10) if future is not None else 0


def test_startup_self_check_registers_missing_commands_and_handlers():
    transport = TelegramPolling("test-token")
    for command in ("start", "stop", "menu", "orders", "status"):
        transport.command(command, lambda *_: "ok")
    transport.on_post(lambda _post: None)
    transport.on_idle(lambda: None)
    calls = []

    def call(method, values):
        calls.append((method, values))
        if method == "getMe":
            return {"ok": True, "result": {"id": 1}}
        if method == "getMyCommands":
            return {"ok": True, "result": []}
        return {"ok": True, "result": True}

    transport._call = call
    transport._startup_self_check()
    assert [method for method, _ in calls] == ["getMe", "getMyCommands", "setMyCommands"]
    assert calls[-1][1]["commands"]
    assert transport.connected


def test_application_registers_exact_public_commands_and_main_menu_buttons():
    database = Database(":memory:")
    app = Application.__new__(Application)
    app.database = database
    app.telegram_transport = TelegramPolling("test-token", {7})
    app.telegram_feed = object()
    app._register_telegram_handlers()
    assert app.telegram_transport.registered_commands == ("menu", "orders", "start", "status", "stop")
    assert set(app.telegram_transport._menu_handlers) == {
        "📋 Заказы", "📊 Статус", "▶️ Запустить", "⏹ Остановить",
    }
    assert app.telegram_transport._post_handler is not None
    assert app.telegram_transport._idle_handler is not None
    assert app.telegram_transport._callback_handler is not None
    database.close()


def test_startup_self_check_repairs_wrong_commands_and_skips_correct_commands():
    transport = TelegramPolling("test-token")
    for command in ("start", "stop", "menu", "orders", "status"):
        transport.command(command, lambda *_: "ok")
    transport.on_post(lambda _post: None)
    transport.on_idle(lambda: None)
    for current, expected_methods in (
        ([{"command": "start", "description": "wrong"}], ["getMe", "getMyCommands", "setMyCommands"]),
        (list(transport.EXPECTED_COMMANDS), ["getMe", "getMyCommands"]),
    ):
        calls = []

        def call(method, values):
            calls.append((method, values))
            return {"ok": True, "result": {"id": 1}} if method == "getMe" else {"ok": True, "result": current}

        transport._call = call
        transport._startup_self_check()
        assert [method for method, _ in calls] == expected_methods


def test_startup_self_check_rejects_missing_local_handlers():
    transport = TelegramPolling("test-token")
    for command in ("start", "stop", "menu", "orders"):
        transport.command(command, lambda *_: "ok")
    transport.on_post(lambda _post: None)
    transport.on_idle(lambda: None)
    try:
        transport._startup_self_check()
    except RuntimeError as error:
        assert "handlers are incomplete" in str(error)
    else:
        raise AssertionError("incomplete command handlers must fail self-check")


def test_startup_self_check_failure_is_logged_and_polling_still_starts():
    transport = TelegramPolling("test-token")
    diagnostics, polling = [], []
    transport.on_self_check_error(diagnostics.append)
    transport._startup_self_check = lambda: (_ for _ in ()).throw(RuntimeError("failed"))
    transport._poll = lambda: polling.append(True)
    transport._run()
    assert diagnostics == ["startup_self_check_failed"]
    assert polling == [True]


def test_start_stop_only_change_durable_delivery_flag_and_menu():
    database = Database(":memory:")
    app, _sent = build_app(database)
    collectors_running = app.telegram.running
    stopped = app._stop_command(7, "")
    assert app.notifications_enabled is False
    assert app.telegram.running is collectors_running is True
    assert database.get_setting("bot_enabled") == "0"
    assert "сбор заказов на пк продолжается" in stopped.text.casefold()
    assert app._main_menu()["keyboard"][-1] == [{"text": "▶️ Запустить"}]
    restarted, _ = build_app(database, enabled=database.get_setting("bot_enabled") == "1")
    assert restarted.notifications_enabled is False

    started = app._start_command(7, "")
    assert app.notifications_enabled is True
    assert app.telegram.running is True
    assert database.get_setting("bot_enabled") == "1"
    assert "⏹ Остановить" in str(app._main_menu())
    assert "Проверка недоставленных заказов" in started.text
    assert started.reply_markup == app._main_menu()
    wait_for_fill(app)
    database.close()


def test_menu_always_returns_a_fresh_persistent_keyboard():
    database = Database(":memory:")
    app, _ = build_app(database, enabled=False)
    response = app._menu_command(7, "")
    assert response.reply_markup["keyboard"] == [
        [{"text": "📋 Заказы"}, {"text": "📊 Статус"}],
        [{"text": "▶️ Запустить"}],
    ]
    callback = app._telegram_callback(7, "menu")
    assert callback.messages[0].reply_markup == response.reply_markup
    database.close()


def test_orders_delivers_20_and_repeat_delivers_zero():
    database = Database(":memory:")
    app, sent = build_app(database)
    for number in range(20):
        save_order(database, number)
    first = app._orders_command(7, "")
    wait_for_fill(app)
    second = app._orders_command(7, "")
    wait_for_fill(app)
    assert len(sent) == 20
    assert "Историю Telegram" in first.text
    assert "Историю Telegram" in second.text
    assert database.connection.execute("SELECT COUNT(*) FROM telegram_order_deliveries").fetchone()[0] == 20
    database.close()


def test_orders_with_zero_candidates_sends_only_no_new_orders_reply():
    database = Database(":memory:")
    app, sent = build_app(database)
    response = app._orders_command(7, "")
    assert "Историю Telegram" in response.text
    assert sent == []
    database.close()


def test_orders_sends_only_unsent_among_latest_70_candidates():
    database = Database(":memory:")
    app, sent = build_app(database)
    orders = [save_order(database, number, published_hours=number / 10 + 0.1) for number in range(70)]
    for order in orders[:40]:
        database.reserve_telegram_order_delivery(order.id, 7)
        database.complete_telegram_order_delivery(order.id, 7, "old")
    response = app._orders_command(7, "")
    wait_for_fill(app)
    assert "Историю Telegram" in response.text
    assert len(sent) == 30
    assert database.connection.execute("SELECT COUNT(*) FROM telegram_order_deliveries").fetchone()[0] == 70
    database.close()


def test_orders_considers_only_70_most_recent_by_published_at():
    import re

    database = Database(":memory:")
    app, sent = build_app(database)
    for number in range(120):
        save_order(database, number, published_hours=(120 - number) / 10)
    response = app._orders_command(7, "")
    wait_for_fill(app)
    sent_texts = [text for _chat, text, _markup in sent]
    assert "Историю Telegram" in response.text
    assert len(sent) == 70
    assert all(not any(f"Order text {number}\n" in text for text in sent_texts) for number in range(50))
    assert all(any(f"Order text {number}\n" in text for text in sent_texts) for number in range(50, 120)), [
        number for number in range(50, 120) if not any(f"Order text {number}\n" in text for text in sent_texts)
    ]
    delivered_numbers = [int(re.search(r"Order text (\d+)", text).group(1)) for text in sent_texts]
    assert delivered_numbers == sorted(delivered_numbers)
    database.close()


def test_orders_uses_collected_time_and_saved_matched_state_only():
    database = Database(":memory:")
    app, sent = build_app(database)
    save_order(database, "old", collected_hours=13)
    save_order(database, "unknown", classification=OrderClassification.UNDETERMINED)
    save_order(database, "archived", status=OrderStatus.ARCHIVED)
    save_order(database, "recent")
    response = app._orders_command(7, "")
    wait_for_fill(app)
    assert "Историю Telegram" in response.text
    assert len(sent) == 1
    assert "Order text recent" in sent[0][1]
    database.close()


def test_orders_excludes_non_telegram_and_archived_orders_remain_openable():
    database = Database(":memory:")
    app, sent = build_app(database)
    save_order(database, "active")
    database.save_order(Order(
        "marketplace", "kwork", "kwork:1", "Marketplace", "", "https://example.test",
        classification=OrderClassification.MATCHED,
    ))
    archived = save_order(database, 99, status=OrderStatus.ARCHIVED)
    response = app._orders_command(7, "")
    wait_for_fill(app)
    assert "Историю Telegram" in response.text
    assert len(sent) == 1
    callback = app._telegram_callback(7, f"feed:card:m:{archived.id}")
    assert callback.edit is not None
    assert any(button["text"] == "🔗 Оригинал" for row in callback.edit.reply_markup["inline_keyboard"] for button in row)
    database.close()


def test_orders_considers_active_orders_only_even_when_archive_has_no_delivery_rows():
    database = Database(":memory:")
    app, sent = build_app(database)
    for number in range(50):
        save_order(database, f"active-{number}")
    archived = [save_order(database, f"archived-{number}", status=OrderStatus.ARCHIVED) for number in range(30)]
    assert all(database.telegram_order_delivery(order.id, 7) is None for order in archived)

    response = app._orders_command(7, "")
    wait_for_fill(app)
    assert "Историю Telegram" in response.text
    assert len(sent) == 50
    assert all(database.telegram_order_delivery(order.id, 7) is None for order in archived)
    database.close()


def test_archived_card_keeps_original_contact_and_menu_buttons():
    database = Database(":memory:")
    app, _sent = build_app(database)
    archived = Order(
        "archived-card", "telegram", "channel:archived", "Archived", "Body",
        "https://t.me/source/55", status=OrderStatus.ARCHIVED,
        metadata={
            "author_entity_type": "user", "author_user_id": 55,
            "author_username": "client_user", "author_contact_url": "https://t.me/client_user",
            "original_message_url": "https://t.me/source/55",
        },
    )
    database.save_order(archived)
    result = app._telegram_callback(7, f"feed:card:m:{archived.id}")
    buttons = [button for row in result.edit.reply_markup["inline_keyboard"] for button in row]
    assert {button["text"] for button in buttons} == {"✉️ Написать", "🔗 Оригинал", "🏠 Меню"}
    assert database.get_order(archived.id).status == OrderStatus.ARCHIVED

    edited = []
    answers = []
    app.telegram_transport.on_callback(app._telegram_callback)
    app.telegram_transport.edit_message = lambda chat, message, text, markup: edited.append((chat, message, text, markup)) or True
    app.telegram_transport.answer_callback = lambda callback_id, text="": answers.append((callback_id, text)) or True
    assert app.telegram_transport._handle_update({
        "callback_query": {
            "id": "archived-card-callback", "from": {"id": 7},
            "data": f"feed:card:m:{archived.id}",
            "message": {"message_id": 30, "chat": {"id": 7}},
        },
    }) is True
    assert edited and "Автор:" in edited[0][2]
    assert answers == [("archived-card-callback", "")]
    database.close()


def test_status_archive_count_reflects_archived_orders():
    database = Database(":memory:")
    app, _sent = build_app(database)
    save_order(database, "old-status", status=OrderStatus.ARCHIVED)
    response = app._status_command(7, "")
    assert "Архив: 1" in response.text
    database.close()


def test_start_catches_up_only_missing_orders_and_does_not_restart_collector():
    database = Database(":memory:")
    app, sent = build_app(database, enabled=False)
    orders = [save_order(database, number) for number in range(3)]
    database.reserve_telegram_order_delivery(orders[0].id, 7)
    database.complete_telegram_order_delivery(orders[0].id, 7, "existing")
    app.telegram.running = False
    response = app._start_command(7, "")
    wait_for_fill(app)
    assert response.text.startswith("Получение заказов в Telegram-боте включено.")
    assert "Проверка недоставленных заказов" in response.text
    assert len(sent) == 2
    assert app.telegram.running is False
    assert response.reply_markup == app._main_menu()
    database.close()


def test_repeated_start_coalesces_fill_and_never_restarts_user_api():
    database = Database(":memory:")
    app, sent = build_app(database, enabled=False)
    saved = save_order(database, "single-start")
    user_api_starts = []
    app.telegram_user = SimpleNamespace(
        configured=True, connected=False, running=True, delivery_barrier_active=False,
        start=lambda: user_api_starts.append("start"),
    )
    replies = [app._start_command(7, "") for _ in range(5)]
    wait_for_fill(app)
    assert len(sent) == 1
    assert database.telegram_order_delivery(saved.id, 7)["status"] == "sent"
    assert user_api_starts == []
    assert all(reply.reply_markup == app._main_menu() for reply in replies)
    assert database.get_setting("bot_enabled") == "1"
    database.close()


def test_desktop_startup_owns_collectors_and_restores_saved_delivery_state():
    database = Database(":memory:")
    database.set_setting("bot_enabled", "1")
    app = Application.__new__(Application)
    app.database = database
    app.notifications_enabled = True
    started = []
    app.telegram = SimpleNamespace(running=True, last_error="", start=lambda: started.append("bot-api"))
    app.telegram_transport = SimpleNamespace(start=lambda: started.append("polling"))
    app.telegram_user = SimpleNamespace(
        running=True, delivery_barrier_active=False, start=lambda: started.append("user-api"),
    )
    fills = []
    app._request_delivery_fill = lambda reason: fills.append(reason)
    app.start_services()
    assert started == ["bot-api", "polling", "user-api"]
    assert fills == ["desktop_startup"]
    assert database.get_setting("bot_enabled", "") == "1"
    database.close()


def test_orders_command_and_menu_button_share_manual_history_handler():
    database = Database(":memory:")
    app, sent = build_app(database)
    history_requests = []
    app.telegram_user = SimpleNamespace(
        configured=True, connected=True, delivery_barrier_active=False,
        request_manual_history=lambda: history_requests.append("manual") or "scheduled",
    )
    app._register_telegram_handlers()
    command_handler = app.telegram_transport._handlers["orders"]
    button_handler = app.telegram_transport._menu_handlers["📋 Заказы"]
    assert command_handler.__self__ is button_handler.__self__ is app
    assert command_handler.__func__ is button_handler.__func__ is Application._orders_command
    direct = app._orders_command(7, "")
    button_update = {"message": {"chat": {"id": 7}, "from": {"id": 7}, "text": "📋 Заказы"}}
    assert app.telegram_transport._handle_update(button_update)
    assert history_requests == ["manual", "manual"]
    assert "Ручная проверка Telegram запущена" in direct.text
    assert sent == []
    database.close()


def test_orders_while_stopped_can_request_history_without_enabling_delivery():
    database = Database(":memory:")
    app, sent = build_app(database, enabled=False)
    history_requests = []
    app.telegram_user = SimpleNamespace(
        configured=True, connected=True, delivery_barrier_active=False,
        request_manual_history=lambda: history_requests.append(True) or "scheduled",
    )
    response = app._orders_command(7, "")
    assert history_requests == [True]
    assert app.notifications_enabled is False
    assert database.get_setting("bot_enabled", "0") == "0"
    assert "Получение карточек остановлено" in response.text
    assert sent == []
    database.close()


def test_realtime_order_deferred_by_barrier_is_not_lost_outside_latest_70():
    database = Database(":memory:")
    app, sent = build_app(database)
    deferred = save_order(database, "realtime-deferred", published_hours=11.9, collected_hours=1)
    for number in range(70):
        save_order(database, f"history-{number}", published_hours=10 - number / 100, collected_hours=1)
    app.telegram_user.delivery_barrier_active = True
    app._notify_new_order(deferred)
    assert sent == []
    app._flush_user_api_delivery_batch("manual")
    assert len(sent) == 71
    assert database.telegram_order_delivery(deferred.id, 7)["status"] == "sent"
    database.close()


def test_stopped_bot_saves_orders_but_does_not_deliver_until_start():
    database = Database(":memory:")
    app, sent = build_app(database, enabled=False)
    order = save_order(database, "paused")
    app._notify_new_order(order)
    assert sent == []
    assert database.telegram_order_delivery(order.id, 7) is None
    app._start_command(7, "")
    wait_for_fill(app)
    assert len(sent) == 1
    assert database.telegram_order_delivery(order.id, 7)["status"] == "sent"
    database.close()


def test_card_only_shows_available_contact_original_and_menu_buttons():
    database = Database(":memory:")
    app, _ = build_app(database)
    order = save_order(database, 1)
    card = app.telegram_feed.new_order(7, order)
    buttons = [button for row in card.reply_markup["inline_keyboard"] for button in row]
    assert [button["text"] for button in buttons] == ["🔗 Оригинал", "🏠 Меню"]
    database.close()


def test_deleted_order_callback_is_safe_and_deactivates_old_keyboard():
    database = Database(":memory:")
    app, _ = build_app(database)
    callback = app._telegram_callback(7, "feed:text:m:deleted-order:0")
    assert callback.notice == "Заказ уже удалён или недоступен."
    assert callback.edit.reply_markup == {"inline_keyboard": []}
    transport = app.telegram_transport
    transport._callback_handler = app._telegram_callback
    answers, edits = [], []
    transport.answer_callback = lambda _id, text="": answers.append(text) or True
    transport.edit_message = lambda _chat, _message, text, markup=None: edits.append((text, markup)) or True
    assert transport.process_update({
        "update_id": 8,
        "callback_query": {
            "id": "callback", "from": {"id": 7}, "data": "feed:card:m:deleted-order",
            "message": {"message_id": 4, "chat": {"id": 7}},
        },
    })
    assert edits == [("Заказ уже удалён или недоступен.", {"inline_keyboard": []})]
    assert answers == ["Заказ уже удалён или недоступен."]
    database.close()


def test_status_uses_saved_metrics_when_user_api_is_disconnected():
    database = Database(":memory:")
    app, _ = build_app(database)
    save_order(database, "match")
    save_order(database, "unknown", classification=OrderClassification.UNDETERMINED)
    save_order(database, "archived", status=OrderStatus.ARCHIVED)
    app.telegram_transport.connected = True
    app.telegram_user.connected = False
    app.telegram_user.configured = False
    status = app.status_text()
    assert "Бот: 🟢 запущен" in status
    assert "Bot API: 🟢" in status
    assert "User API: 🟡 не настроен" in status
    assert "Подходящих: 1" in status
    assert "Не определено: 1" in status
    assert "Архив: 1" in status
    assert "Источники: 0 / 0" in status
    database.close()
