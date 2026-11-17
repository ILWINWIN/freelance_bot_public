"""Sequential TXT orchestration for Telegram source additions."""

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from threading import Event, Lock


class ImportRowStatus(StrEnum):
    """Outcome of one prepared TXT row."""

    ADDED = "ADDED"
    DUPLICATE = "DUPLICATE"
    INVALID = "INVALID"
    NO_ACCESS = "NO_ACCESS"
    ERROR = "ERROR"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class ImportRowResult:
    """Safe result for one source reference; errors never contain secrets."""

    line: int
    reference: str
    status: ImportRowStatus
    reason: str = ""


@dataclass
class ImportProgress:
    """Thread-safe progress shared by the desktop worker and Qt polling."""

    total: int = 0
    processed: int = 0
    added: int = 0
    duplicate: int = 0
    no_access: int = 0
    invalid: int = 0
    errors: int = 0
    cancelled: bool = False
    _lock: Lock = field(default_factory=Lock, repr=False)

    def snapshot(self) -> dict[str, int | bool]:
        """Return a consistent UI-safe copy without exposing source secrets."""
        with self._lock:
            return {
                "total": self.total, "processed": self.processed, "added": self.added,
                "duplicate": self.duplicate, "no_access": self.no_access,
                "invalid": self.invalid, "errors": self.errors, "cancelled": self.cancelled,
            }

    def set_total(self, total: int) -> None:
        with self._lock:
            self.total = total

    def record(self, status: ImportRowStatus) -> None:
        with self._lock:
            self.processed += 1
            if status is ImportRowStatus.ADDED:
                self.added += 1
            elif status is ImportRowStatus.DUPLICATE:
                self.duplicate += 1
            elif status is ImportRowStatus.NO_ACCESS:
                self.no_access += 1
            elif status is ImportRowStatus.INVALID:
                self.invalid += 1
            elif status is ImportRowStatus.ERROR:
                self.errors += 1

    def mark_cancelled(self) -> None:
        with self._lock:
            self.cancelled = True


@dataclass(frozen=True, slots=True)
class ImportResult:
    """Completed or cancelled import summary."""

    total: int
    processed: int
    added: int
    duplicate: int
    no_access: int
    invalid: int
    errors: int
    cancelled: bool
    rows: tuple[ImportRowResult, ...] = ()


class SourceImportService:
    """Prepare TXT references and delegate every valid row to Application."""

    def __init__(self, application) -> None:
        self.application = application

    def import_file(self, path: str, progress: ImportProgress | None = None,
                    cancel_event: Event | None = None) -> ImportResult:
        """Read a UTF-8/BOM TXT file and run the canonical source pipeline."""
        text = Path(path).read_text(encoding="utf-8-sig")
        return self.import_text(text, progress, cancel_event)

    def import_bytes(self, content: bytes, progress: ImportProgress | None = None,
                     cancel_event: Event | None = None) -> ImportResult:
        """Decode downloaded Bot API content and use the same orchestration."""
        return self.import_text(content.decode("utf-8-sig"), progress, cancel_event)

    def import_text(self, text: str, progress: ImportProgress | None = None,
                    cancel_event: Event | None = None) -> ImportResult:
        """Process prepared rows sequentially; no resolve/save logic is duplicated here."""
        progress = progress or ImportProgress()
        cancel_event = cancel_event or Event()
        rows = self._prepare_rows(text.lstrip("\ufeff"))
        progress.set_total(len(rows))
        results: list[ImportRowResult] = []
        for index, (line_number, reference) in enumerate(rows):
            if cancel_event.is_set():
                progress.mark_cancelled()
                results.extend(
                    ImportRowResult(pending_line, pending_reference, ImportRowStatus.CANCELLED, "Импорт отменён")
                    for pending_line, pending_reference in rows[index:]
                )
                break
            try:
                before = self._source_keys()
                source, resolved = self.application.add_telegram_source_reference(reference)
                if not resolved:
                    status = ImportRowStatus.NO_ACCESS
                    reason = str(source.access_status or "Источник недоступен")
                else:
                    after_key = (str(source.chat_id), str(source.source_id))
                    status = ImportRowStatus.DUPLICATE if after_key in before or any(
                        key[0] == str(source.chat_id) or (source.source_id and key[1] == str(source.source_id))
                        for key in before
                    ) else ImportRowStatus.ADDED
                    reason = "Источник уже добавлен" if status is ImportRowStatus.DUPLICATE else ""
            except ValueError as exc:
                status = ImportRowStatus.INVALID
                reason = str(exc)
            except Exception as exc:  # Per-row isolation; never expose exception text or secrets.
                status = ImportRowStatus.ERROR
                reason = type(exc).__name__
            results.append(ImportRowResult(line_number, reference, status, reason))
            progress.record(status)
        return self._result(progress, results)

    def _source_keys(self) -> set[tuple[str, str]]:
        snapshot = self.application.telegram_source_snapshot()
        return {(str(source.chat_id), str(source.source_id)) for source in snapshot.values()}

    def _prepare_rows(self, text: str) -> list[tuple[int, str]]:
        rows: list[tuple[int, str]] = []
        seen: set[str] = set()
        for line_number, raw in enumerate(text.splitlines(), 1):
            reference = raw.strip()
            if not reference or reference.startswith("#"):
                continue
            try:
                key = self.application._normalize_telegram_source_reference(reference)
            except ValueError:
                key = reference.casefold()
            if key in seen:
                continue
            seen.add(key)
            rows.append((line_number, key))
        return rows

    @staticmethod
    def _result(progress: ImportProgress, rows: list[ImportRowResult]) -> ImportResult:
        values = progress.snapshot()
        return ImportResult(
            int(values["total"]), int(values["processed"]), int(values["added"]),
            int(values["duplicate"]), int(values["no_access"]), int(values["invalid"]),
            int(values["errors"]), bool(values["cancelled"]), tuple(rows),
        )
