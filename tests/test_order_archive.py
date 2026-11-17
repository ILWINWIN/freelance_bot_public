"""Collection-age archival lifecycle tests using isolated local SQLite files."""

import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

from freelance_bot.app import Application
from freelance_bot.core.models import DispatchJob, JobStatus, Order, OrderClassification, OrderStatus, ProposalMode, TelegramMessageEnvelope
from freelance_bot.core.order_service import OrderService
from freelance_bot.storage.database import Database


def _temporary_database_path():
    descriptor, path = tempfile.mkstemp(prefix="order-archive-", suffix=".sqlite3", dir=".")
    os.close(descriptor)
    return path


def _remove_database_files(path):
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except FileNotFoundError:
            pass


def test_archive_expiry_uses_12_hours_from_collection_and_normalizes_timezones():
    database = Database(":memory:")
    now = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
    recent = Order(
        "recent", "telegram", "chat:1", "Recent", "body", "https://t.me/channel/1",
        published_at=now - timedelta(days=20), discovered_at=now - timedelta(hours=11, minutes=59),
    )
    expired = Order(
        "expired", "telegram", "chat:2", "Expired", "body", "https://t.me/channel/2",
        published_at=now - timedelta(minutes=1), discovered_at=(now - timedelta(hours=12, minutes=1)).replace(tzinfo=None),
        metadata={"original_text": "kept", "author_contact_url": "https://t.me/client_user"},
    )
    database.save_order(recent)
    database.save_order(expired)
    assert database.reserve_telegram_order_delivery(expired.id, 7)
    database.complete_telegram_order_delivery(expired.id, 7, "telegram-card-20")

    assert database.archive_expired_orders(now) == 1
    assert database.get_order("recent").status == OrderStatus.ACTIVE
    archived = database.get_order("expired")
    assert archived.status == OrderStatus.ARCHIVED
    assert archived.metadata["original_text"] == "kept"
    assert archived.metadata["author_contact_url"] == "https://t.me/client_user"
    assert database.get_order("expired").url == expired.url
    assert database.telegram_order_delivery(expired.id, 7)["telegram_message_id"] == "telegram-card-20"
    assert database.archive_expired_orders(now) == 0
    database.close()


def test_exact_12_hour_boundary_is_archived_but_11_hours_59_minutes_is_not():
    database = Database(":memory:")
    now = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
    for order_id, age in (("boundary", timedelta(hours=12)), ("one_minute_left", timedelta(hours=11, minutes=59))):
        database.save_order(Order(
            order_id, "telegram", order_id, order_id, "body", "",
            discovered_at=now - age,
        ))
    assert database.archive_expired_orders(now) == 1
    assert database.get_order("boundary").status == OrderStatus.ARCHIVED
    assert database.get_order("one_minute_left").status == OrderStatus.ACTIVE
    database.close()


def test_time_based_archive_does_not_remove_order_or_dispatch_history():
    database = Database(":memory:")
    now = datetime.now(timezone.utc)
    order = Order("order-with-job", "telegram", "chat:job", "Old order", "", "", discovered_at=now - timedelta(hours=13))
    database.save_order(order)
    job = DispatchJob("existing-job", order.id, "account", ProposalMode.DRAFT, status=JobStatus.AWAITING_CONFIRMATION)
    database.save_job(job)

    assert database.archive_expired_orders(now) == 1
    assert database.get_order(order.id).status == OrderStatus.ARCHIVED
    assert database.load_jobs()[0].id == job.id
    database.close()


def test_repeated_historical_intake_preserves_first_collection_time():
    database = Database(":memory:")
    now = datetime.now(timezone.utc)
    original_collection = now - timedelta(hours=13)
    old = Order(
        "stable-order", "telegram", "-10055:90", "Need project", "Нужно разработать проект",
        "https://t.me/source/90", discovered_at=original_collection,
        classification=OrderClassification.MATCHED,
    )
    database.save_order(old)
    service = OrderService(database)
    repeat = service.intake_telegram_message(TelegramMessageEnvelope(
        "-10055", "90", "Нужно разработать проект", "Source", "channel", "source", now,
    ))
    assert repeat.is_new is False
    assert repeat.order.collected_at == original_collection
    assert database.archive_expired_orders(now) == 1
    assert database.get_order(old.id).status == OrderStatus.ARCHIVED
    database.close()


def test_legacy_database_without_collection_time_does_not_use_published_at():
    path = _temporary_database_path()
    try:
        connection = sqlite3.connect(path)
        connection.execute(
            """CREATE TABLE orders (
                id TEXT PRIMARY KEY, platform_id TEXT, external_id TEXT,
                title TEXT, description TEXT, url TEXT, budget REAL,
                payload TEXT, published_at TEXT
            )"""
        )
        connection.execute(
            "INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?)",
            ("legacy", "telegram", "old:1", "Legacy", "", "", None, "{}", "2020-01-01T00:00:00+00:00"),
        )
        connection.commit()
        connection.close()

        database = Database(path)
        legacy = database.get_order("legacy")
        assert legacy.published_at == datetime(2020, 1, 1, tzinfo=timezone.utc)
        assert legacy.collected_at >= datetime.now(timezone.utc) - timedelta(minutes=1)
        assert database.archive_expired_orders() == 0
        database.close()
    finally:
        _remove_database_files(path)


def test_application_startup_archives_orders_that_expired_while_closed(monkeypatch):
    path = _temporary_database_path()
    try:
        seed = Database(path)
        now = datetime.now(timezone.utc)
        seed.save_order(Order(
            "expired-on-restart", "telegram", "chat:44", "Old", "Original text", "https://t.me/source/44",
            published_at=now - timedelta(minutes=5), discovered_at=now - timedelta(hours=13),
            metadata={"original_text": "still stored"},
        ))
        seed.close()
        monkeypatch.setenv("DATABASE_PATH", path)

        app = Application()
        archived = app.database.get_order("expired-on-restart")
        assert archived.status == OrderStatus.ARCHIVED
        assert archived.metadata["original_text"] == "still stored"
        assert archived.url == "https://t.me/source/44"
        app.database.close()
    finally:
        _remove_database_files(path)
