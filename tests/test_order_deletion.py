"""Persistent manual deletion, undo safety, and Telegram tombstone regressions."""

from datetime import datetime, timedelta, timezone

from freelance_bot.core.deletion_service import DeletionService
from freelance_bot.core.models import Order, OrderStatus, TelegramMessageEnvelope
from freelance_bot.core.order_service import OrderService
from freelance_bot.storage.database import Database


class Clock:
    """Inject wall and monotonic clocks for deterministic deletion-window tests."""

    def __init__(self):
        self.now = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
        self.tick = 100.0

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)
        self.tick += seconds


def archived_order(database, order_id, external_id=None):
    """Persist a minimal archived Order with a stable Telegram identity."""
    order = Order(
        order_id, "telegram", external_id or f"-100123:{order_id}",
        f"Order {order_id}", "Нужно сделать проект", "https://t.me/example/1",
        status=OrderStatus.ARCHIVED,
    )
    database.save_order(order)
    return order


def setup_service(delay=300):
    database = Database(":memory:")
    clock = Clock()
    service = DeletionService(
        database, now_fn=lambda: clock.now, monotonic_fn=lambda: clock.tick,
        delay_seconds=delay,
    )
    return database, clock, service


def test_archived_order_schedules_once_and_active_order_is_rejected():
    database, _, service = setup_service()
    archived_order(database, "archived")
    database.save_order(Order("active", "telegram", "chat:active", "Active", "", ""))

    result = service.schedule_deletion(["archived", "active", "missing"])

    assert (result.scheduled, result.rejected_not_archived, result.not_found) == (1, 1, 1)
    assert database.get_order("archived").status == OrderStatus.ARCHIVED
    assert database.get_order("active").status == OrderStatus.ACTIVE
    assert len(service.list_pending_deletions()) == 1
    assert database.telegram_runtime_metrics()["pending_deletions"] == 1
    try:
        database.archive_order("archived", OrderStatus.ACTIVE)
    except ValueError as exc:
        assert "отмените ожидающее удаление" in str(exc)
    else:
        raise AssertionError("A queued deletion must not restore an Order to ACTIVE")
    database.close()


def test_repeated_schedule_does_not_duplicate_or_reset_deadline_or_batch():
    database, clock, service = setup_service()
    archived_order(database, "same")
    first = service.schedule_deletion(["same"])
    original = service.list_pending_deletions()[0]
    clock.advance(60)

    repeated = service.schedule_deletion(["same"])
    current = service.list_pending_deletions()[0]

    assert repeated.scheduled == 0
    assert repeated.already_pending == 1
    assert current["delete_after"] == original["delete_after"]
    assert current["batch_id"] == original["batch_id"] == first.batch_id
    clock.advance(240)
    assert service.process_due_deletions() == 1
    assert database.get_order("same") is None
    database.close()


def test_bulk_uses_one_deadline_and_batch_and_supports_partial_cancel():
    database, _, service = setup_service()
    archived_order(database, "one")
    archived_order(database, "two")
    result = service.schedule_deletion(["one", "two"])
    rows = service.list_pending_deletions()

    assert result.scheduled == 2
    assert {row["batch_id"] for row in rows} == {result.batch_id}
    assert len({row["delete_after"] for row in rows}) == 1
    assert service.cancel_deletion(["one"]) == 1
    assert service.cancel_deletion_batch(result.batch_id) == 1
    assert service.list_pending_deletions() == []
    assert database.get_order("one").status == OrderStatus.ARCHIVED
    assert database.get_order("two").status == OrderStatus.ARCHIVED
    database.close()


def test_startup_recovery_resets_a_full_window_on_each_restart():
    database, clock, first = setup_service()
    archived_order(database, "restart")
    first.schedule_deletion(["restart"])
    clock.advance(120)

    second_runtime_start = clock.tick
    second = DeletionService(
        database, now_fn=lambda: clock.now,
        monotonic_fn=lambda: clock.tick - second_runtime_start,
    )
    assert second.recover_pending_deletions_on_startup() == 1
    recovered_deadline = second.list_pending_deletions()[0]["delete_after"]
    assert datetime.fromisoformat(recovered_deadline) == clock.now + timedelta(minutes=5)
    clock.advance(120)

    third_runtime_start = clock.tick
    third = DeletionService(
        database, now_fn=lambda: clock.now,
        monotonic_fn=lambda: clock.tick - third_runtime_start,
    )
    third.recover_pending_deletions_on_startup()
    assert datetime.fromisoformat(third.list_pending_deletions()[0]["delete_after"]) == clock.now + timedelta(minutes=5)
    clock.advance(299)
    assert third.process_due_deletions() == 0
    assert database.get_order("restart") is not None
    clock.advance(1)
    assert third.process_due_deletions() == 1
    database.close()


def test_wall_clock_jump_forward_cannot_bypass_monotonic_five_minutes():
    database, clock, service = setup_service()
    archived_order(database, "clock-jump")
    service.schedule_deletion(["clock-jump"])
    clock.now += timedelta(hours=3)
    clock.tick += 5

    assert service.process_due_deletions() == 0
    assert database.get_order("clock-jump") is not None
    clock.tick += 295
    assert service.process_due_deletions() == 1
    database.close()


def test_due_delete_is_atomic_and_rolls_back_tombstones_and_children_on_failure():
    database, clock, service = setup_service()
    order = archived_order(database, "rollback")
    database.connection.execute(
        "INSERT INTO telegram_order_deliveries(order_id,recipient_id,status,updated_at) VALUES(?,?,?,?)",
        (order.id, "7", "sent", clock.now.isoformat()),
    )
    database.connection.execute(
        "INSERT INTO desktop_drafts(order_id,text,updated_at) VALUES(?,?,?)",
        (order.id, "draft", clock.now.isoformat()),
    )
    database.connection.commit()
    service.schedule_deletion([order.id])
    clock.advance(300)
    database.connection.execute(
        "CREATE TRIGGER fail_order_delete BEFORE DELETE ON orders BEGIN SELECT RAISE(ABORT, 'forced'); END"
    )

    assert service.process_due_deletions() == 0
    assert database.get_order(order.id) is not None
    assert database.connection.execute("SELECT COUNT(*) FROM order_deletion_queue").fetchone()[0] == 1
    assert database.connection.execute("SELECT COUNT(*) FROM deleted_order_tombstones").fetchone()[0] == 0
    assert database.connection.execute("SELECT COUNT(*) FROM telegram_order_deliveries").fetchone()[0] == 1
    assert database.connection.execute("SELECT COUNT(*) FROM desktop_drafts").fetchone()[0] == 1
    assert "Ошибка операции SQLite" in database.list_order_deletions()[0]["last_error"]

    database.connection.execute("DROP TRIGGER fail_order_delete")
    database.connection.commit()
    assert service.process_due_deletions() == 1
    assert database.get_order(order.id) is None
    assert database.connection.execute("SELECT COUNT(*) FROM telegram_order_deliveries").fetchone()[0] == 0
    assert database.connection.execute("SELECT COUNT(*) FROM desktop_drafts").fetchone()[0] == 0
    assert database.connection.execute("SELECT COUNT(*) FROM order_deletion_queue").fetchone()[0] == 0
    database.close()


def test_unfinished_dispatch_or_sending_delivery_blocks_but_sent_does_not():
    database, clock, service = setup_service()
    blocked = archived_order(database, "blocked")
    sent = archived_order(database, "sent")
    database.connection.execute(
        "INSERT INTO dispatch_jobs(id,order_id,account_id,mode,status,next_attempt_at) VALUES(?,?,?,?,?,?)",
        ("job", blocked.id, "account", "manual", "sending", clock.now.isoformat()),
    )
    database.connection.execute(
        "INSERT INTO telegram_order_deliveries(order_id,recipient_id,status,updated_at) VALUES(?,?,?,?)",
        (blocked.id, "7", "sending", clock.now.isoformat()),
    )
    database.connection.execute(
        "INSERT INTO telegram_order_deliveries(order_id,recipient_id,status,updated_at) VALUES(?,?,?,?)",
        (sent.id, "7", "sent", clock.now.isoformat()),
    )
    database.connection.commit()
    service.schedule_deletion([blocked.id, sent.id])
    clock.advance(300)

    assert service.process_due_deletions() == 1
    assert database.get_order(sent.id) is None
    assert database.get_order(blocked.id) is not None
    assert "незавершённая" in database.list_order_deletions()[0]["last_error"]
    database.connection.execute("UPDATE dispatch_jobs SET status='failed' WHERE id='job'")
    database.connection.execute("UPDATE telegram_order_deliveries SET status='sent' WHERE order_id=?", (blocked.id,))
    database.connection.commit()
    assert service.process_due_deletions() == 1
    assert database.get_order(blocked.id) is None
    database.close()


def test_hard_delete_tombstones_canonical_and_alias_and_intake_cannot_resurrect_them():
    database, clock, service = setup_service()
    order = archived_order(database, "canonical", "-100123:77")
    database.connection.execute(
        """INSERT INTO telegram_message_aliases(platform_id,external_id,canonical_order_id,content_fingerprint,observed_at)
        VALUES('telegram','-100123:78',?,'fingerprint',?)""",
        (order.id, clock.now.isoformat()),
    )
    database.connection.commit()
    service.schedule_deletion([order.id])
    clock.advance(300)
    assert service.process_due_deletions() == 1

    columns = {row["name"] for row in database.connection.execute("PRAGMA table_info(deleted_order_tombstones)")}
    assert columns == {"platform_id", "external_id", "deleted_at"}
    assert database.is_deleted_telegram_message("-100123:77")
    assert database.is_deleted_telegram_message("-100123:78")
    service = OrderService(database)
    bot_result = service.intake_telegram_post({
        "chat": {"id": -100123, "title": "Source", "type": "channel"},
        "message": {"message_id": 77, "text": "Нужно сделать сайт. Бюджет 30000."},
    })
    user_result = service.intake_telegram_message(TelegramMessageEnvelope(
        "-100123", "77", "Нужно сделать сайт. Бюджет 30000.", "Source", "channel",
    ))
    alias_result = service.intake_telegram_message(TelegramMessageEnvelope(
        "-100123", "78", "Нужно сделать сайт. Бюджет 30000.", "Source", "channel",
    ))
    assert bot_result.order is None and bot_result.drop_reason == "skipped_deleted_order"
    assert user_result.order is None and user_result.drop_reason == "skipped_deleted_order"
    assert alias_result.order is None and alias_result.drop_reason == "skipped_deleted_order"
    assert database.count("orders") == 0
    database.close()


def test_duplicate_intake_while_pending_preserves_archived_state_and_queue_deadline():
    database, clock, service = setup_service()
    order = archived_order(database, "pending", "-100123:90")
    service.schedule_deletion([order.id])
    original = service.list_pending_deletions()[0]["delete_after"]
    result = OrderService(database).intake_telegram_message(TelegramMessageEnvelope(
        "-100123", "90", "Нужно сделать сайт. Бюджет 30000.", "Source", "channel",
    ))

    assert result.order is not None and not result.is_new
    assert result.order.status == OrderStatus.ARCHIVED
    assert service.list_pending_deletions()[0]["delete_after"] == original
    assert database.count("orders") == 1
    database.close()


def test_legacy_archive_cleanup_cannot_bypass_an_active_deletion_window():
    database, clock, service = setup_service()
    order = archived_order(database, "retention-guard")
    service.schedule_deletion([order.id])
    database.connection.execute(
        "UPDATE orders SET archived_at='2020-01-01T00:00:00+00:00' WHERE id=?", (order.id,)
    )
    database.connection.commit()

    assert database.cleanup_archived_orders(clock.now) == 0
    assert database.get_order(order.id) is not None
    assert len(service.list_pending_deletions()) == 1
    database.close()


def test_existing_database_migration_is_idempotent_and_keeps_orders():
    database = Database(":memory:")
    order = archived_order(database, "migration")

    database.initialize()
    database.initialize()

    assert database.get_order(order.id) is not None
    assert database.connection.execute("SELECT COUNT(*) FROM order_deletion_queue").fetchone()[0] == 0
    assert database.connection.execute("SELECT COUNT(*) FROM deleted_order_tombstones").fetchone()[0] == 0
    database.close()
