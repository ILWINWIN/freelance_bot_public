"""Qt model and lightweight delegates for the order feed."""

from PySide6.QtCore import QAbstractTableModel, Qt, QRect
from PySide6.QtGui import QColor, QFont, QPen, QPixmap
from PySide6.QtWidgets import QStyledItemDelegate
from datetime import datetime, timezone
from math import ceil
from pathlib import Path


ASSET_DIR = Path(__file__).with_name('assets')
SOURCE_ICON_FILES = {'kwork': 'kwork.png', 'telegram': 'telegram.png', 'fl.ru': 'flru.png'}
_SOURCE_PIXMAPS = {}


def source_pixmap(source):
    """Load a supplied platform mark lazily after Qt is initialized."""
    key = source.casefold()
    filename = SOURCE_ICON_FILES.get(key)
    if not filename:
        return None
    if key not in _SOURCE_PIXMAPS:
        _SOURCE_PIXMAPS[key] = QPixmap(str(ASSET_DIR / filename))
    return _SOURCE_PIXMAPS[key]


def publication_text(order):
    """Distinguish source publication from the persisted discovery fallback."""
    stamp = order.published_at or order.discovered_at
    minutes = max(0, int((datetime.now().astimezone() - stamp.astimezone()).total_seconds() // 60))
    text = "только что" if minutes < 1 else f"{minutes} мин" if minutes < 60 else f"{minutes // 60} ч" if minutes < 1440 else stamp.astimezone().strftime("%d.%m %H:%M")
    return text if order.published_at else "Обн. " + text


def budget_text(order):
    """Format only known monetary information without guessing the currency."""
    if order.budget is None:
        return 'Не указано'
    amount = f'{order.budget:,.0f}' if float(order.budget).is_integer() else f'{order.budget:,.2f}'
    return amount.replace(',', ' ') + ' ' + (order.currency or 'валюта не указана')


def author_text(order):
    """Show an explicit author supplied by the source."""
    return str(order.metadata.get('author_name') or order.metadata.get('author_username') or 'Не указано')


def event_time_text(event):
    """Format a persisted event time compactly without changing its source value."""
    raw = str(event.get('created_at') or '')
    try:
        stamp = datetime.fromisoformat(raw.replace('Z', '+00:00')).astimezone()
    except ValueError:
        return raw or 'Не указано'
    now = datetime.now().astimezone()
    return stamp.strftime('%H:%M') if stamp.date() == now.date() else stamp.strftime('%d.%m %H:%M')


def event_status_kind(status):
    """Map known event outcomes to a semantic color without inventing success."""
    value = str(status or '').casefold()
    if value in {'ok', 'success', 'succeeded', 'успех'}:
        return 'ok'
    if value in {'failed', 'error', 'ошибка'}:
        return 'error'
    if value in {'started', 'running', 'working', 'в работе'}:
        return 'running'
    return 'warning'


class EventLogModel(QAbstractTableModel):
    """Expose a bounded, safe event history for the desktop event journal."""

    HEADERS = ('Время', 'Результат', 'Действие', 'Сервис')

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items = []

    def replace_items(self, items):
        """Replace only the currently filtered event rows."""
        self.beginResetModel()
        self.items = list(items)
        self.endResetModel()

    def rowCount(self, parent=None):
        """Return the number of visible event rows."""
        return 0 if parent is not None and parent.isValid() else len(self.items)

    def columnCount(self, parent=None):
        """Return the four compact event-journal columns."""
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        """Supply accessible names even when the compact header is hidden."""
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section]

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        """Return safe fields; message details are deliberately never rendered."""
        if not index.isValid():
            return None
        event = self.items[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return (
                event_time_text(event),
                str(event.get('status') or 'Не указано'),
                str(event.get('action') or 'Не указано'),
                str(event.get('service') or 'Система'),
            )[index.column()]
        if role == Qt.ItemDataRole.ToolTipRole and event.get('operation_id'):
            return 'Идентификатор операции: ' + str(event['operation_id'])
        return None


class FeedModel(QAbstractTableModel):
    """Expose order rows plus one expanded action row, with stable order IDs."""

    HEADERS = ('Заказ', 'Источник', 'Автор', 'Бюджет', 'Отклики', 'Время', 'Статус')

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items = []
        self.rows = []
        self.selected_id = None
        self.pending_deletions = {}

    def replace_items(self, items, selected_id):
        """Replace a page; the view restores its selection by ID."""
        self.beginResetModel()
        self.items = items
        self.selected_id = selected_id
        self.rows = []
        for item in items:
            self.rows.append((item, False))
            if item[0].id == selected_id:
                self.rows.append((item, True))
        self.endResetModel()

    def set_pending_deletions(self, pending):
        """Cache the latest queue snapshot so countdown reads never query SQLite."""
        self.pending_deletions = dict(pending)

    def refresh_deletion_countdowns(self):
        """Notify visible status cells to repaint from their cached deadlines."""
        for row, ((order, group, _), expanded) in enumerate(self.rows):
            if not expanded and group == 'archive' and order.id in self.pending_deletions:
                index = self.index(row, 6)
                self.dataChanged.emit(index, index, [Qt.ItemDataRole.DisplayRole])

    def rowCount(self, parent=None):
        """Return visible rows including the expanded action row."""
        return 0 if parent is not None and parent.isValid() else len(self.rows)

    def columnCount(self, parent=None):
        """Return the seven reference columns."""
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        """Supply localized column labels."""
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section]

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        """Return presentation values without performing IO."""
        if not index.isValid():
            return None
        (order, group, status), expanded = self.rows[index.row()]
        if role == Qt.ItemDataRole.UserRole:
            return order.id
        if role == Qt.ItemDataRole.BackgroundRole and order.id == self.selected_id:
            return QColor('#103f79')
        if role == Qt.ItemDataRole.ToolTipRole:
            if index.column() == 5:
                stamp = order.published_at or order.discovered_at
                return ('Опубликован: ' if order.published_at else 'Обнаружен: ') + stamp.astimezone().strftime('%d.%m.%Y %H:%M')
            if index.column() == 6 and order.id in self.pending_deletions:
                return str(self.pending_deletions[order.id].get('last_error') or 'Удаление можно отменить до окончания обратного отсчёта.')
            return order.title if index.column() == 0 else str(self.data(index) or '')
        if role == Qt.ItemDataRole.DisplayRole and not expanded:
            pending = self.pending_deletions.get(order.id) if index.column() == 6 else None
            if pending is not None:
                if pending.get('last_error'):
                    status = 'Ошибка удаления' if str(pending['last_error']).startswith('Ошибка операции SQLite') else 'Удаление заблокировано'
                else:
                    try:
                        deadline = datetime.fromisoformat(str(pending['delete_after']))
                        if deadline.tzinfo is None:
                            deadline = deadline.replace(tzinfo=timezone.utc)
                        seconds = max(0, ceil((deadline.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()))
                        status = f'Удаление через {seconds // 60:02}:{seconds % 60:02}' if seconds else 'Удаление…'
                    except (TypeError, ValueError, KeyError):
                        status = 'Ожидает удаления'
            return (order.title, str(order.metadata.get('source_name') or order.platform_id),
                    author_text(order), budget_text(order), str(order.metadata.get('responses', '—')),
                    publication_text(order), status)[index.column()]
        return None


class FeedDelegate(QStyledItemDelegate):
    """Paint compact text and status badges without per-cell widget overhead."""

    def paint(self, painter, option, index):
        """Render a row with elided text, separators and a labeled status."""
        painter.save()
        model = index.model()
        (order, group, status), expanded = model.rows[index.row()]
        selected = order.id == model.selected_id
        painter.fillRect(option.rect, QColor('#103f79' if selected else '#0e1d2c' if index.row() % 2 else '#0c1a28'))
        painter.setPen(QColor('#223549'))
        painter.drawLine(option.rect.bottomLeft(), option.rect.bottomRight())
        if not expanded:
            rect = option.rect.adjusted(5, 0, -3, 0) if index.column() == 1 else option.rect.adjusted(10, 0, -8, 0)
            text = str(index.data() or '')
            if index.column() == 0:
                # The marker speeds up scanning; title and status remain available as text.
                marker = QRect(rect.left(), rect.center().y() - 5, 10, 10)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor('#1197ff'))
                painter.drawEllipse(marker)
                rect.setLeft(rect.left() + 20)
            if index.column() == 6:
                color = '#43df8c' if group == 'sent' else '#90a8c2' if group == 'archive' else '#ffc55a' if group == 'review' else '#88c8ff'
                painter.setBrush(QColor('#153d36' if group == 'sent' else '#343023' if group == 'review' else '#123459'))
                painter.setPen(QPen(QColor(color), 0.5))
                badge = QRect(rect.x(), rect.y() + 8, min(rect.width(), option.fontMetrics.horizontalAdvance(text) + 18), 24)
                painter.drawRoundedRect(badge, 12, 12)
                painter.drawText(badge.adjusted(8, 0, -6, 0), Qt.AlignmentFlag.AlignVCenter, option.fontMetrics.elidedText(text, Qt.TextElideMode.ElideRight, badge.width()-14))
            else:
                font = QFont(option.font)
                font.setBold(index.column() == 0 and selected)
                painter.setFont(font)
                painter.setPen(QColor('#edf4ff' if index.column() == 0 else '#bcd0e8'))
                if index.column() == 1:
                    pixmap = source_pixmap(text)
                    if pixmap and not pixmap.isNull():
                        size = min(23, rect.height() - 4)
                        icon_rect = QRect(rect.x(), rect.y() + (rect.height() - size) // 2, size, size)
                        painter.drawPixmap(icon_rect, pixmap)
                        rect.setLeft(rect.left() + size + 6)
                painter.drawText(rect, Qt.AlignmentFlag.AlignVCenter, painter.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, rect.width()))
        painter.restore()


class EventLogDelegate(QStyledItemDelegate):
    """Paint event rows with readable outcome colors and no per-cell widgets."""

    STATUS_COLORS = {
        'ok': '#43df8c',
        'error': '#ff6d7a',
        'running': '#88c8ff',
        'warning': '#ffc55a',
    }

    def paint(self, painter, option, index):
        """Draw compact journal rows that remain readable at small panel heights."""
        painter.save()
        background = '#132538' if index.row() % 2 else '#102131'
        painter.fillRect(option.rect, QColor(background))
        painter.setPen(QColor('#243a50'))
        painter.drawLine(option.rect.bottomLeft(), option.rect.bottomRight())
        text = str(index.data() or '')
        color = self.STATUS_COLORS[event_status_kind(text)] if index.column() == 1 else '#c7d9ee'
        painter.setPen(QColor(color))
        rect = option.rect.adjusted(10, 0, -8, 0)
        painter.drawText(
            rect,
            Qt.AlignmentFlag.AlignVCenter,
            painter.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, rect.width()),
        )
        painter.restore()
