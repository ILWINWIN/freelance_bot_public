from dataclasses import replace
from threading import Event
from types import SimpleNamespace

from freelance_bot.core.models import TelegramSource
from freelance_bot.core.desktop_service import DesktopService
from freelance_bot.core.source_import import ImportProgress, ImportRowStatus, SourceImportService


class FakeApplication:
    """Small canonical-pipeline fake; the importer must never save directly."""

    def __init__(self):
        self.telegram_sources = {}
        self.calls = []
        self.no_access = {"@blocked_jobs"}
        self.fail = {"@error_jobs"}

    def _normalize_telegram_source_reference(self, value):
        value = value.strip()
        if value.startswith("https://t.me/"):
            value = "@" + value.rsplit("/", 1)[-1]
        if value.startswith("t.me/"):
            value = "@" + value.rsplit("/", 1)[-1]
        if not value.startswith("@") and len(value) >= 6:
            value = "@" + value
        if not value.startswith("@") or len(value) < 6:
            raise ValueError("Некорректный Telegram reference")
        return value.casefold()

    def telegram_source_snapshot(self):
        return dict(self.telegram_sources)

    def add_telegram_source_reference(self, reference):
        self.calls.append(reference)
        if not reference.startswith("@"):
            raise ValueError("Некорректный Telegram reference")
        if reference in self.fail:
            raise RuntimeError("temporary failure")
        chat_id = f"-100{abs(hash(reference)) % 1000000}"
        existing = self.telegram_sources.get(chat_id)
        if existing:
            return existing, True
        source = TelegramSource(chat_id, reference, reference not in self.no_access, source_id=f"sid-{chat_id}")
        if reference in self.no_access:
            source = replace(source, pending_connection=True, access_status="forbidden", enabled=False)
            self.telegram_sources[chat_id] = source
            return source, False
        self.telegram_sources[chat_id] = source
        return source, True


def test_import_prepares_utf8_bom_comments_blanks_and_duplicates() -> None:
    app = FakeApplication()
    result = SourceImportService(app).import_text("\ufeff\n# jobs\n @Python_Jobs \nhttps://t.me/python_jobs\n\n")
    assert result.total == 1
    assert result.added == 1
    assert app.calls == ["@python_jobs"]


def test_import_accepts_bare_username() -> None:
    app = FakeApplication()
    result = SourceImportService(app).import_text("backend_jobs")
    assert result.added == 1
    assert app.calls == ["@backend_jobs"]


def test_import_processes_100_rows_through_canonical_pipeline() -> None:
    app = FakeApplication()
    text = "\n".join(f"@source_{i:03d}" for i in range(100))
    result = SourceImportService(app).import_text(text)
    assert result.total == result.processed == 100
    assert result.added == 100
    assert len(app.calls) == 100


def test_import_keeps_bad_rows_and_no_access_isolated() -> None:
    app = FakeApplication()
    result = SourceImportService(app).import_text("@good_jobs\nwrong\n@blocked_jobs\n@error_jobs\n@later_jobs")
    statuses = [row.status for row in result.rows]
    assert statuses == [ImportRowStatus.ADDED, ImportRowStatus.INVALID, ImportRowStatus.NO_ACCESS, ImportRowStatus.ERROR, ImportRowStatus.ADDED]
    assert result.processed == 5
    assert result.added == 2 and result.no_access == 1 and result.invalid == 1 and result.errors == 1


def test_import_repeat_is_duplicate_and_keeps_source_id() -> None:
    app = FakeApplication()
    importer = SourceImportService(app)
    first = importer.import_text("@python_jobs")
    source_id = next(iter(app.telegram_sources.values())).source_id
    second = importer.import_text("https://t.me/python_jobs")
    assert first.added == 1
    assert second.duplicate == 1
    assert next(iter(app.telegram_sources.values())).source_id == source_id


def test_import_cancellation_keeps_completed_rows() -> None:
    app = FakeApplication()
    cancel = Event()
    progress = ImportProgress()

    original = app.add_telegram_source_reference

    def add_and_cancel(reference):
        result = original(reference)
        cancel.set()
        return result

    app.add_telegram_source_reference = add_and_cancel
    result = SourceImportService(app).import_text("@first_jobs\n@second_jobs\n@third_jobs", progress, cancel)
    assert result.cancelled is True
    assert result.processed == 1
    assert result.added == 1
    assert [row.status for row in result.rows[1:]] == [ImportRowStatus.CANCELLED, ImportRowStatus.CANCELLED]
    assert len(app.telegram_sources) == 1


def test_desktop_import_delegates_to_the_shared_import_service() -> None:
    calls = []

    class Importer:
        def import_file(self, path, progress, cancel_event):
            calls.append((path, progress, cancel_event))
            return "done"

    service = DesktopService.__new__(DesktopService)
    service.app = SimpleNamespace(source_importer=Importer())
    assert service.import_telegram_sources("sources.txt", "progress", "cancel") == "done"
    assert calls == [("sources.txt", "progress", "cancel")]
