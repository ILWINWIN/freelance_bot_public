"""Desktop contract and interaction tests with isolated, offline data."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from time import monotonic

import pytest
pytest.importorskip("PySide6")
pytest.importorskip("PySide6.QtCore")
from PySide6.QtCore import Qt, QItemSelectionModel
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QFileDialog, QPushButton, QTabWidget
from PySide6.QtTest import QTest

from freelance_bot.accounts.manager import AccountManager
from freelance_bot.ai.proposals import ProposalGenerator
from freelance_bot.core.desktop_service import DesktopService, feed_state
from freelance_bot.core.models import Order, OrderClassification, OrderStatus, ProposalProfile, DispatchJob, ProposalMode, JobStatus, SearchProfile, TelegramMessageEnvelope, TelegramSource
from freelance_bot.core.order_service import OrderService
from freelance_bot.core.source_import import ImportResult, ImportRowStatus
from freelance_bot.storage.database import Database
from freelance_bot.ui.parsing_dialog import ParsingSettingsDialog, SourceToggleButton
from freelance_bot.ui.window import Dashboard, SourceImportProgressDialog


def wait_until(predicate, timeout=5):
    """Process real Qt events until asynchronous work completes."""
    deadline = monotonic() + timeout
    while not predicate() and monotonic() < deadline:
        QApplication.processEvents()
        QTest.qWait(15)
    assert predicate()


@pytest.fixture
def service():
    database = Database(":memory:")
    app = SimpleNamespace(
        database=database,
        accounts=AccountManager(database=database),
        telegram=SimpleNamespace(configured=False, running=False),
        telegram_transport=SimpleNamespace(connected=False),
        telegram_sources={},
        orders=SimpleNamespace(profile=SearchProfile("default", "Все заказы"), set_profile=lambda profile: None),
        proposal_profile=ProposalProfile("default", "Основной"),
        proposals=ProposalGenerator(),
        browser=SimpleNamespace(memory_guard=SimpleNamespace(soft_limit_mb=4096, hard_limit_mb=5120)),
        shutdown=lambda: None,
    )
    service = DesktopService(app)
    yield service
    database.close()


@pytest.fixture
def window(service):
    app = QApplication.instance() or QApplication([])
    for i in range(65):
        service.app.database.save_order(Order(
            str(i), "telegram", str(i), f"Заказ {i}", "Нужно разработать приложение",
            "https://example.test/order", budget=15000 + i, currency="RUB",
            published_at=datetime(2026, 9, 18, 1, 0, tzinfo=timezone.utc),
            metadata={"source_name": "Telegram", "author_name": "Алексей"},
        ))
    window = Dashboard(service=service)
    window.show()
    wait_until(lambda: bool(window.data))
    yield window
    window.close()
    wait_until(lambda: window.closed)
    app.processEvents()


@pytest.mark.parametrize("status,group", [
    ("pending", "review"), ("sending", "review"), ("unknown_result", "review"),
    ("failed", "review"), ("sent", "sent"), ("succeeded", "sent"),
])
def test_feed_state_never_invents_success(status, group):
    order = Order("o", "test", "1", "Title", "", "")
    assert feed_state(order, {}, {"o": {"status": status}})[0] == group
    order.status = OrderStatus.ARCHIVED
    assert feed_state(order, {}, {"o": {"status": status}})[0] == "archive"


def test_undetermined_order_uses_its_own_feed_group(service):
    order = Order("undetermined", "test", "1", "Title", "", "", classification=OrderClassification.UNDETERMINED)
    service.app.database.save_order(order)
    snapshot = service.snapshot()
    assert snapshot["items"][0][1] == "undetermined"
    assert snapshot["counts"]["undetermined"] == 1


def test_manual_desktop_archive_moves_order_from_active_to_archive(service):
    order = Order("manual-archive", "telegram", "chat:1", "Manual", "", "")
    service.app.database.save_order(order)

    service.archive(order.id)

    snapshot = service.snapshot()
    assert service.app.database.get_order(order.id).status == OrderStatus.ARCHIVED
    assert not any(item[0].id == order.id and item[1] != "archive" for item in snapshot["items"])
    assert any(item[0].id == order.id and item[1] == "archive" for item in snapshot["items"])


def test_archive_delete_requires_confirmation_then_shows_pending_countdown(window, service, monkeypatch):
    order = Order(
        "delete-ui", "telegram", "delete-ui", "Тестовый архивный заказ", "Нужно сделать приложение", "",
        status=OrderStatus.ARCHIVED,
    )
    service.app.database.save_order(order)
    window.refresh()
    wait_until(lambda: not window.snapshot_pending and "delete-ui" in window.data.get("pending_deletions", {}) or
               any(item[0].id == order.id for item in window.data.get("items", [])))
    window.tabs.setCurrentIndex(window.GROUPS.index("archive"))
    row = next(i for i, ((item, _, _), expanded) in enumerate(window.model.rows) if item.id == order.id and not expanded)
    window.table.selectionModel().select(
        window.model.index(row, 0),
        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
    )
    assert window.deletion_selection == {order.id}

    monkeypatch.setattr(QMessageBox, "question", lambda *_args, **_kwargs: QMessageBox.StandardButton.No)
    window._delete_selected_archive_orders()
    assert service.app.database.list_order_deletions() == []

    monkeypatch.setattr(QMessageBox, "question", lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes)
    window._delete_selected_archive_orders()
    wait_until(lambda: order.id in window.data.get("pending_deletions", {}))
    assert service.app.database.get_order(order.id).status == OrderStatus.ARCHIVED
    row = next(i for i, ((item, _, _), expanded) in enumerate(window.model.rows) if item.id == order.id and not expanded)
    assert "Удаление через" in window.model.index(row, 6).data()
    assert window.cancel_selected_deletions_button.isEnabled()


def test_archive_select_all_uses_only_visible_order_ids_and_filter_clears_selection(window, service):
    orders = [
        Order(f"bulk-{number}", "telegram", f"bulk-{number}", title, "", "", status=OrderStatus.ARCHIVED)
        for number, title in ((1, "Видимый тест"), (2, "Видимый проект"), (3, "Скрытый архив"))
    ]
    for order in orders:
        service.app.database.save_order(order)
    window.refresh()
    wait_until(lambda: all(any(item[0].id == order.id for item in window.data.get("items", [])) for order in orders))
    window.tabs.setCurrentIndex(window.GROUPS.index("archive"))
    window.search.setText("Видимый")
    visible_ids = {order.id for order, _, _ in window.model.items}
    assert visible_ids == {"bulk-1", "bulk-2"}
    window._select_all_archive_visible()
    assert window.deletion_selection == visible_ids
    window.search.setText("Скрытый")
    assert window.deletion_selection == set()
    assert {order.id for order, _, _ in window.model.items} == {"bulk-3"}


def test_archive_selection_restores_by_order_id_after_reordering(window, service):
    first = Order("stable-a", "telegram", "stable-a", "A", "", "", status=OrderStatus.ARCHIVED)
    second = Order("stable-b", "telegram", "stable-b", "B", "", "", status=OrderStatus.ARCHIVED)
    service.app.database.save_order(first)
    service.app.database.save_order(second)
    window.refresh()
    wait_until(lambda: all(any(item[0].id == order.id for item in window.data.get("items", [])) for order in (first, second)))
    window.tabs.setCurrentIndex(window.GROUPS.index("archive"))
    window.deletion_selection = {second.id}
    window._apply_deletion_selection()
    reversed_items = list(reversed(window.model.items))
    window.model.replace_items(reversed_items, window.selected_id)
    window._apply_deletion_selection()
    selected = {
        str(index.data(Qt.ItemDataRole.UserRole))
        for index in window.table.selectionModel().selectedRows(0)
    }
    assert selected == {second.id}


def test_undetermined_order_is_visible_only_on_its_tab(window, service):
    service.app.database.save_order(Order(
        "undetermined-tab", "telegram", "undetermined-tab", "Без правил", "", "",
        classification=OrderClassification.UNDETERMINED,
    ))
    window.refresh()
    wait_until(lambda: not window.snapshot_pending and window.data["counts"]["undetermined"] == 1)
    window.tabs.setCurrentIndex(window.GROUPS.index("undetermined"))
    assert window.model.rowCount() == 1
    assert window.model.index(0, 0).data() == "Без правил"
    window.tabs.setCurrentIndex(window.GROUPS.index("new"))
    assert all(row[0].id != "undetermined-tab" for row, _expanded in window.model.rows)


def test_periodic_archive_timer_moves_expired_order_to_desktop_archive(window, service):
    now = datetime.now(timezone.utc)
    order = Order(
        "timer-expired", "telegram", "timer:1", "Expired by timer", "", "",
        published_at=now, discovered_at=now - timedelta(hours=13),
    )
    service.app.database.save_order(order)

    assert window.archive_timer.interval() == 5 * 60 * 1000
    window._archive_expired_orders()
    wait_until(lambda: service.app.database.get_order(order.id).status == OrderStatus.ARCHIVED)
    wait_until(lambda: window.data.get("counts", {}).get("archive") == 1)
    window.tabs.setCurrentIndex(window.GROUPS.index("archive"))
    assert any(row[0].id == order.id for row, _expanded in window.model.rows)


def test_filter_rules_save_through_desktop_service(service):
    service.save_search_profile("Python\nбот\npython", "вакансия, полная занятость")
    profile = service.app.database.load_search_profile()
    assert profile.include_keywords == ["Python", "бот"]
    assert profile.exclude_keywords == ["вакансия", "полная занятость"]


def test_telegram_gate_rules_are_saved_and_applied_without_restart(service):
    service.save_search_profile(
        "python", "crypto",
        telegram_hard_stops="не публиковать",
        telegram_order_signals="новый заказ",
    )
    profile = service.app.database.load_search_profile()
    assert profile.telegram_gate_configured is True
    assert profile.telegram_hard_stops == ["не публиковать"]
    assert profile.telegram_order_signals == ["новый заказ"]

    orders = OrderService(service.app.database, profile)
    dropped = orders.intake_telegram_message(TelegramMessageEnvelope("1", "1", "Новый заказ, не публиковать"))
    accepted = orders.intake_telegram_message(TelegramMessageEnvelope("1", "2", "Новый заказ для сайта"))
    assert dropped.order is None
    assert dropped.drop_reason == "hard_stop:custom"
    assert accepted.order is not None


def test_draft_does_not_enqueue_or_send(service):
    db = service.app.database
    db.save_order(Order("o", "test", "1", "Title", "", ""))
    service.save_draft("o", "private text")
    assert db.count("dispatch_jobs") == 0
    assert db.desktop_state()[0]["o"] == "private text"
    assert service.snapshot()["items"][0][1] == "review"
    assert db.list_logs() == []


def test_journal_snapshot_hides_private_details_but_keeps_operation_id(service):
    db = service.app.database
    operation_id = "0d6a4d4c02c742a5a7fd0c92d00c9db3"
    db.log("system", "open_original", "ok", operation_id)
    db.log("telegram", "source_check", "warning", "private message text")
    journal = service.snapshot()["logs"]
    assert journal[0]["operation_id"] == ""
    assert journal[1]["operation_id"] == operation_id
    assert all("details" not in event for event in journal)
    assert "private message text" not in str(journal)


def test_journal_filters_are_safe_and_interactive(window, service):
    db = service.app.database
    operation_id = "b5f7d5c024d448339e1a927f69da407a"
    db.log("system", "open_original", "ok", operation_id)
    db.log("telegram", "source_check", "warning", "private message text")
    window.refresh()
    wait_until(lambda: not window.snapshot_pending and window.log_model.rowCount() == 2)
    assert window.log_model.data(window.log_model.index(1, 2)) == "Открыт оригинал заказа"
    assert window.log_filter.maximumWidth() == 220
    window.log_filter.setText("Открыт оригинал")
    assert window.log_model.rowCount() == 1
    window.log_filter.setText(operation_id)
    assert window.log_model.rowCount() == 1
    window.log_filter.setText("private message text")
    assert window.log_model.rowCount() == 0


def test_journal_collapses_to_bottom_tab_without_moving_columns(window):
    horizontal_sizes = window.workspace.sizes()
    expanded_height = window.vertical.sizes()[1]
    workspace_top = window.workspace.geometry().top()
    workspace_width = window.workspace.width()
    window._toggle_logs()
    assert not window.logs_expanded
    assert not window.logs_panel.isVisible()
    assert window.log_tab.isVisible()
    assert window.log_tab.parentWidget() is window.journal_dock
    assert window.journal_dock.isVisible()
    assert window.log_tab.geometry().left() == 0
    assert window.journal_dock.geometry().top() >= window.vertical.geometry().bottom()
    assert window.workspace.sizes() == horizontal_sizes
    assert window.workspace.geometry().top() == workspace_top
    assert window.workspace.width() == workspace_width
    window._toggle_logs()
    assert window.logs_expanded
    assert window.logs_panel.isVisible()
    assert not window.log_tab.isVisible()
    assert not window.journal_dock.isVisible()
    assert window.vertical.sizes()[1] >= min(expanded_height, window.log_height) - 2
    assert window.workspace.sizes() == horizontal_sizes


def test_parsing_status_opens_functional_settings_dialog(window):
    window.parsing_label.click()
    wait_until(lambda: hasattr(window, "parsing_dialog") and window.parsing_dialog.isVisible())
    dialog = window.parsing_dialog
    assert dialog.windowTitle() == "Настройки парсинга"
    assert dialog.toggle_button.text() == "Запустить парсинг"
    assert dialog.marketplaces_table.rowCount() == 3
    assert dialog.tabs.tabText(0) == "Биржи и сайты"
    assert dialog.tabs.tabText(1) == "Telegram"
    assert dialog.sources_card[1].text() == "0 из 0 активно"
    assert dialog.telegram_table.rowCount() == 0
    assert dialog.autostart.isChecked()


def test_telegram_sources_page_exposes_visible_txt_import_button(window):
    """The user-facing Telegram source tab, not the secondary dialog, owns import."""
    window._navigate(1)
    QApplication.processEvents()
    page = window.pages.widget(1)
    tabs = page.findChild(QTabWidget)
    tabs.setCurrentIndex(0)
    texts = {item.text(): item for item in page.findChildren(QPushButton)}
    assert "+ Добавить источник" in texts
    assert "Импортировать TXT" in texts
    assert texts["+ Добавить источник"].isVisible()
    assert texts["Импортировать TXT"].isVisible()


def test_txt_import_file_dialog_cancel_is_a_noop(window, monkeypatch):
    requested = []
    monkeypatch.setattr(window, "_start_source_import", requested.append)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args, **_kwargs: ("", ""))
    window._choose_source_import_file()
    assert requested == []


def test_txt_import_file_is_sent_to_existing_worker(window, monkeypatch):
    requested = []
    monkeypatch.setattr(window, "_start_source_import", requested.append)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args, **_kwargs: ("C:/tmp/sources.txt", "Text files (*.txt)"))
    window._choose_source_import_file()
    assert requested == ["C:/tmp/sources.txt"]


def test_txt_import_progress_dialog_updates_and_cancels(window):
    dialog = SourceImportProgressDialog(window)
    dialog.show()
    QApplication.processEvents()
    values = {"total": 100, "processed": 37, "added": 20, "duplicate": 8,
              "no_access": 4, "invalid": 3, "errors": 2}
    dialog.update_progress(values)
    assert dialog.progress_bar.maximum() == 100
    assert dialog.progress_bar.value() == 37
    assert "Обработано: 37 / 100" in dialog.counters.text()
    cancelled = []
    dialog.cancel_requested.connect(lambda: cancelled.append(True))
    dialog.cancel_button.click()
    assert cancelled == [True]
    dialog.close()


def test_txt_import_uses_desktop_service_worker_and_keeps_final_summary(window, monkeypatch):
    calls = []

    def fake_import(path, progress, cancel_event):
        calls.append(path)
        progress.set_total(1)
        progress.record(ImportRowStatus.ADDED)
        return ImportResult(1, 1, 1, 0, 0, 0, 0, False)

    monkeypatch.setattr(window.service, "import_telegram_sources", fake_import)
    window._start_source_import("C:/tmp/sources.txt")
    wait_until(lambda: window._source_import_progress is None)
    assert calls == ["C:/tmp/sources.txt"]
    assert window._source_import_dialog.status.text().startswith("Импорт завершён")


def test_parsing_autostart_requires_confirmation_when_enabled(window, monkeypatch):
    window._open_parsing_settings()
    dialog = window.parsing_dialog
    snapshot = dict(window.data)
    snapshot["parsing_autostart"] = False
    dialog.set_snapshot(snapshot)
    requested = []
    dialog.autostart_requested.connect(requested.append)
    monkeypatch.setattr(QMessageBox, "question", lambda *_args, **_kwargs: QMessageBox.StandardButton.No)
    dialog.autostart.setChecked(True)
    assert not dialog.autostart.isChecked()
    assert requested == []
    monkeypatch.setattr(QMessageBox, "question", lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes)
    dialog.autostart.setChecked(True)
    assert requested == [True]


def test_source_settings_are_validated_and_persisted(service):
    original = TelegramSource("-100123", "Старое имя", True, "ru", 120)
    service.app.telegram_sources[original.chat_id] = original

    def save_source(source):
        service.app.telegram_sources[source.chat_id] = source
        service.app.database.save_telegram_source(source)

    service.app.add_telegram_source = save_source
    updated = service.update_source(original.chat_id, "Заказы", False, "en", 300)
    persisted = service.app.database.load_telegram_sources()[0]
    assert updated == TelegramSource("-100123", "Заказы", False, "en", 300)
    assert persisted == updated
    with pytest.raises(ValueError):
        service.update_source(original.chat_id, "Заказы", True, "ru", 10)


def test_source_switch_is_persisted_without_overwriting_source_fields(service):
    original = TelegramSource("-100123", "Заказы", True, "en", 300)
    service.app.telegram_sources[original.chat_id] = original

    def save_source(source):
        service.app.telegram_sources[source.chat_id] = source
        service.app.database.save_telegram_source(source)

    service.app.add_telegram_source = save_source
    updated = service.set_source_enabled(original.chat_id, False)
    assert updated == TelegramSource("-100123", "Заказы", False, "en", 300)
    assert service.app.database.load_telegram_sources() == [updated]


def test_desktop_source_addition_uses_the_shared_telegram_source_store(service):
    resolved = TelegramSource("-100123", "Python Freelance", True, "ru", 120, True)

    def resolve(_reference, _name, _enabled, _language, _interval):
        service.app.telegram_sources[resolved.chat_id] = resolved
        service.app.database.save_telegram_source(resolved)
        return resolved, True

    service.app.add_telegram_source_reference = resolve
    added = service.add_source("@python_freelance", "", True, "ru", 120)
    assert added == resolved
    assert service.app.database.load_telegram_sources() == [resolved]
    assert service.app.telegram_sources[resolved.chat_id] == resolved


def test_marketplace_preferences_and_parsing_autostart_are_persisted(service):
    initial = service.snapshot()["marketplaces"]
    assert [item["id"] for item in initial] == ["kwork", "fl.ru", "freelance.ru"]
    assert all(item["status"] == "выключен" for item in initial)
    service.set_marketplace_enabled("kwork", True)
    enabled = service.snapshot()["marketplaces"][0]
    assert enabled["enabled"] is True
    assert enabled["status"] == "не настроено"
    service.add_marketplace("Новая биржа", "https://example.test/jobs")
    assert len(service.snapshot()["marketplaces"]) == 4
    with pytest.raises(ValueError):
        service.add_marketplace("Дубль", "https://example.test/jobs/")
    with pytest.raises(ValueError):
        service.remove_marketplace("kwork")
    service.set_parsing_autostart(False)
    assert service.snapshot()["parsing_autostart"] is False


def test_parsing_dialog_switches_are_explicit_and_telegram_switch_emits_immediately(window):
    window._open_parsing_settings()
    dialog = window.parsing_dialog
    snapshot = dict(window.data)
    source = TelegramSource("-100123", "Заказы", True, "ru", 120)
    snapshot["sources"] = [source]
    dialog.set_snapshot(snapshot)

    marketplace_switch = dialog.marketplaces_table.cellWidget(0, 0).findChild(SourceToggleButton)
    telegram_switch = dialog.telegram_table.cellWidget(0, 0).findChild(SourceToggleButton)
    marketplace_state = dialog.marketplaces_table.cellWidget(0, 0).findChild(QLabel, "sourceState")
    telegram_state = dialog.telegram_table.cellWidget(0, 0).findChild(QLabel, "sourceState")
    assert marketplace_switch.text() == ""
    assert marketplace_state.text() == "Выкл."
    assert marketplace_switch.accessibleName() == "Источник: выключено"
    assert telegram_switch.text() == ""
    assert telegram_state.text() == "Вкл."
    assert telegram_switch.accessibleName() == "Источник: включено"

    requested = []
    dialog.source_toggle_requested.connect(lambda source_id, enabled: requested.append((source_id, enabled)))
    telegram_switch.click()
    assert requested == [("-100123", False)]
    assert telegram_state.text() == "Выкл."


def test_parsing_dialog_preserves_telegram_tab_and_shows_empty_state(window):
    window._open_parsing_settings()
    dialog = window.parsing_dialog
    dialog.tabs.setCurrentIndex(1)
    snapshot = dict(window.data)
    snapshot["sources"] = []
    dialog.set_snapshot(snapshot)
    assert dialog.tabs.currentIndex() == 1
    assert dialog.telegram_area.currentWidget() is dialog.telegram_empty
    assert "Пока нет Telegram" in dialog.telegram_empty.text()


def test_source_deletion_confirmation_names_source_and_respects_cancel(window, monkeypatch):
    window._open_parsing_settings()
    dialog = window.parsing_dialog
    removed = []
    dialog.remove_source_requested.connect(removed.append)
    messages = []
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda _parent, _title, text: messages.append(text) or QMessageBox.StandardButton.No,
    )
    dialog._remove_source("-100123", "Мои заказы")
    assert removed == []
    assert "Мои заказы" in messages[0]
    assert "-100123" in messages[0]

    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    dialog._remove_source("-100123", "Мои заказы")
    assert removed == ["-100123"]


@pytest.mark.parametrize("status", [JobStatus.PENDING, JobStatus.PREPARING, JobStatus.AWAITING_CONFIRMATION, JobStatus.RUNNING, JobStatus.SENDING, JobStatus.UNKNOWN_RESULT])
def test_archive_rejects_unresolved_dispatch(service, status):
    db = service.app.database
    db.save_order(Order("o", "test", "1", "Title", "", ""))
    db.save_job(DispatchJob("j", "o", "a", ProposalMode.AUTO, status=status))
    with pytest.raises(ValueError):
        service.archive("o")
    assert db.get_order("o").status == OrderStatus.ACTIVE


def test_archive_checks_older_unresolved_jobs(service):
    db = service.app.database
    db.save_order(Order("o", "test", "1", "Title", "", ""))
    db.save_job(DispatchJob("first", "o", "a", ProposalMode.AUTO, status=JobStatus.UNKNOWN_RESULT))
    db.save_job(DispatchJob("last", "o", "a", ProposalMode.DRAFT, status=JobStatus.CANCELLED))
    with pytest.raises(ValueError):
        service.archive("o")


def test_restore_and_saved_limits(service):
    db = service.app.database
    db.save_order(Order("o", "test", "1", "Title", "", ""))
    service.archive("o")
    service.archive("o", restore=True)
    assert db.get_order("o").status == OrderStatus.ACTIVE
    service.save_limits(512, 1024)
    service.app.browser.memory_guard.soft_limit_mb = 4096
    DesktopService(service.app)
    assert service.app.browser.memory_guard.soft_limit_mb == 512
    with pytest.raises(ValueError):
        service.save_limits(1024, 512)


def test_pagination_search_selection_and_autosave(window, service):
    assert len(window.model.items) == 50
    window._change_page(1)
    assert len(window.model.items) == 15
    window._change_page(-1)
    selected_id = window.model.items[0][0].id
    window.select_order(selected_id)
    assert sum(expanded for _, expanded in window.model.rows) == 1
    window.editor.setPlainText("Мой черновик")
    other_id = window.model.items[1][0].id
    window.select_order(other_id)
    wait_until(lambda: selected_id not in window.dirty)
    assert service.app.database.desktop_state()[0][selected_id] == "Мой черновик"
    assert window.editor.toPlainText() == ""
    window.select_order(selected_id)
    assert window.editor.toPlainText() == "Мой черновик"
    window.editor.setPlainText("Новая редакция")
    window.refresh()
    wait_until(lambda: not window.snapshot_pending)
    assert window.selected_id == selected_id
    assert window.editor.toPlainText() == "Новая редакция"
    wait_until(lambda: selected_id not in window.dirty)
    window.search.setText("не существует")
    assert window.model.rowCount() == 0
    assert window.feed_notice.isVisible()


def test_currency_sort_and_minimum_layout(window):
    window.sort_filter.setCurrentIndex(2)
    assert "валюту" in window.feed_notice.text()
    window.currency_filter.setCurrentText("RUB")
    assert window.model.items[0][0].budget == 15064
    window.resize(1100, 720)
    QApplication.processEvents()
    assert window.width() == 1100
    assert window.workspace.widget(1).width() >= 385
    window._navigate(1)
    assert window.pages.currentIndex() == 1
    window._navigate(2)
    assert window.pages.currentIndex() == 2


def test_local_generation_updates_and_saves_editor(window, service):
    order_id = window.model.items[0][0].id
    window.select_order(order_id)
    window._generate()
    wait_until(lambda: window.generate_button.isEnabled() and bool(window.editor.toPlainText()))
    wait_until(lambda: order_id not in window.dirty)
    assert "Готов выполнить задачу" in window.editor.toPlainText()
    assert service.app.database.desktop_state()[0][order_id] == window.editor.toPlainText()


def test_close_flushes_last_keystroke(window, service):
    window.select_order("0")
    window.editor.setPlainText("Последний ввод")
    window.close()
    wait_until(lambda: window.closed)
    assert service.app.database.desktop_state()[0]["0"] == "Последний ввод"


def test_save_error_does_not_lose_editor(window, service, monkeypatch):
    window.select_order("0")
    window.editor.setPlainText("Сохранить обязательно")
    original = service.save_draft
    def failure(*args):
        raise OSError("disk full")
    monkeypatch.setattr(service, "save_draft", failure)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: QMessageBox.StandardButton.Cancel)
    window.close()
    wait_until(lambda: not window.closing)
    assert not window.closed
    assert "0" in window.dirty
    assert window.editor.toPlainText() == "Сохранить обязательно"
    monkeypatch.setattr(service, "save_draft", original)
