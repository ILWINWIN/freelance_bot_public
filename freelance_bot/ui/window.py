"""Native Qt dashboard. All persistence and external actions use DesktopService."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event

from PySide6.QtCore import Qt, QTimer, QSize, Signal, QItemSelectionModel
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFrame, QLabel, QPushButton, QDialog,
    QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit, QPlainTextEdit,
    QComboBox, QSpinBox, QTabBar, QTabWidget, QStackedWidget, QSplitter,
    QTableView, QHeaderView, QAbstractItemView, QScrollArea, QListWidget, QCheckBox,
    QListWidgetItem, QMessageBox, QStyle, QFileDialog, QProgressBar,
)

from freelance_bot.core.desktop_service import DesktopService
from freelance_bot.core.source_import import ImportProgress
from freelance_bot.ui.feed import (
    EventLogDelegate,
    EventLogModel,
    FeedDelegate,
    FeedModel,
    author_text,
    budget_text,
)
from freelance_bot.ui.parsing_dialog import ParsingSettingsDialog
from freelance_bot.ui.theme import STYLE


ASSET_DIR = Path(__file__).with_name("assets")


def label(text, name=None, state=None):
    """Create plain text labels so source content cannot become rich markup."""
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if name:
        widget.setObjectName(name)
    if state:
        widget.setProperty("state", state)
    return widget


def button(text, callback, name=None, icon=None):
    """Create a keyboard-accessible action using native Qt icons."""
    widget = QPushButton(text)
    if name:
        widget.setObjectName(name)
    if icon is not None:
        widget.setIcon(icon if isinstance(icon, QIcon) else QApplication.style().standardIcon(icon))
        widget.setIconSize(QSize(20, 20))
    widget.clicked.connect(callback)
    return widget


def panel(name="panel"):
    """Create a themed panel and its vertical content layout."""
    frame = QFrame()
    frame.setObjectName(name)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(12, 12, 12, 12)
    layout.setSpacing(10)
    return frame, layout


class SourceImportProgressDialog(QDialog):
    """Show one non-blocking summary for a running Telegram TXT import."""

    cancel_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Импорт Telegram-источников")
        self.setModal(False)
        self.setMinimumWidth(520)
        self.resize(620, 260)
        layout = QVBoxLayout(self)
        layout.addWidget(label("Импорт Telegram-источников", "title"))
        self.status = label("Подготовка…", "muted")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)
        self.counters = label("Обработано: 0 / 0", "muted")
        self.counters.setWordWrap(True)
        layout.addWidget(self.counters)
        self.errors_button = QPushButton("Показать ошибки")
        self.errors_button.setVisible(False)
        self.errors_button.clicked.connect(self._show_errors)
        layout.addWidget(self.errors_button, 0, Qt.AlignmentFlag.AlignLeft)
        actions = QHBoxLayout()
        actions.addStretch()
        self.cancel_button = QPushButton("Отменить")
        self.cancel_button.clicked.connect(self.cancel_requested.emit)
        actions.addWidget(self.cancel_button)
        self.close_button = QPushButton("Закрыть")
        self.close_button.clicked.connect(self.close)
        self.close_button.setVisible(False)
        actions.addWidget(self.close_button)
        layout.addLayout(actions)
        self._errors = []

    def update_progress(self, values):
        """Render a thread-safe progress snapshot on the Qt thread."""
        total = int(values.get("total", 0))
        processed = int(values.get("processed", 0))
        self.progress_bar.setRange(0, max(total, 1))
        self.progress_bar.setValue(min(processed, max(total, 1)))
        self.counters.setText(
            f"Обработано: {processed} / {total}\n"
            f"Добавлено: {int(values.get('added', 0))} · "
            f"Уже существует: {int(values.get('duplicate', 0))} · "
            f"Нет доступа: {int(values.get('no_access', 0))} · "
            f"Некорректных: {int(values.get('invalid', 0))} · "
            f"Ошибок: {int(values.get('errors', 0))}"
        )

    def finish(self, result):
        """Keep the final summary visible without opening one dialog per row."""
        self._errors = [row for row in result.rows if row.status.value in {"INVALID", "NO_ACCESS", "ERROR"}]
        self.update_progress({
            "total": result.total, "processed": result.processed, "added": result.added,
            "duplicate": result.duplicate, "no_access": result.no_access,
            "invalid": result.invalid, "errors": result.errors,
        })
        state = "Импорт отменён" if result.cancelled else "Импорт завершён"
        self.status.setText(
            f"{state}\nВсего в файле: {result.total}. "
            f"Обработано: {result.processed}. Добавлено: {result.added}."
        )
        self.cancel_button.setVisible(False)
        self.close_button.setVisible(True)
        self.errors_button.setVisible(bool(self._errors))

    def fail(self, message):
        """Display one safe import-level error and leave the application usable."""
        self.status.setText(message)
        self.cancel_button.setVisible(False)
        self.close_button.setVisible(True)

    def _show_errors(self):
        text = "\n".join(f"{row.reference}: {row.reason or row.status.value}" for row in self._errors)
        QMessageBox.information(self, "Ошибки импорта", text or "Ошибок нет.")


class Dashboard(QMainWindow):
    """Present the feed and preserve edits across asynchronous refreshes."""

    GROUPS = ("new", "undetermined", "review", "sent", "archive")
    TAB_NAMES = ("Новые", "Не определено", "На проверке", "Отправлено", "Архив")

    def __init__(self, application=None, *, service=None):
        self.qt_app = QApplication.instance() or QApplication([])
        super().__init__()
        self.service = service or DesktopService(application)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="desktop")
        self._source_import_progress = None
        self._source_import_cancel = None
        self._source_import_dialog = None
        self.pending = []
        self.snapshot_pending = False
        self.data = {}
        self.selected_id = None
        self.deletion_selection: set[str] = set()
        self.local_drafts = {}
        self.dirty = set()
        self.versions = {}
        self.page = 0
        self.closing = False
        self.closed = False
        self.settings_loaded = False
        self.feed_signature = None
        self.log_signature = None
        self.logs_expanded = True
        self.log_height = 170
        self.setWindowTitle("Freelance Bot")
        self.resize(1487, 1058)
        self.setMinimumSize(1100, 720)
        self.setStyleSheet(STYLE)
        self._build()
        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.setInterval(1000)
        self.save_timer.timeout.connect(self._save_current)
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(40)
        self.poll_timer.timeout.connect(self._poll)
        self.poll_timer.start()
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(3000)
        self.refresh_timer.timeout.connect(self.refresh)
        self.refresh_timer.start()
        self.archive_timer = QTimer(self)
        self.archive_timer.setInterval(5 * 60 * 1000)
        self.archive_timer.timeout.connect(self._archive_expired_orders)
        self.archive_timer.start()
        self.deletion_countdown_timer = QTimer(self)
        self.deletion_countdown_timer.setInterval(1000)
        self.deletion_countdown_timer.timeout.connect(self.model.refresh_deletion_countdowns)
        self.deletion_countdown_timer.start()
        self.refresh()

    def _archive_expired_orders(self):
        """Check collection-age expiry occasionally and refresh only on changes."""
        def completed(amount):
            if amount:
                self.refresh()
        self._submit(self.service.archive_expired_orders, completed)

    def _submit(self, operation, callback=None, error=None):
        future = self.executor.submit(operation)
        self.pending.append((future, callback, error))

    def _poll(self):
        if self._source_import_progress is not None:
            values = self._source_import_progress.snapshot()
            if self._source_import_dialog is not None:
                self._source_import_dialog.update_progress(values)
            if hasattr(self, "parsing_dialog"):
                self.parsing_dialog.set_import_progress(values)
        for entry in list(self.pending):
            future, callback, error = entry
            if not future.done():
                continue
            self.pending.remove(entry)
            try:
                result = future.result()
            except Exception as exc:
                if error:
                    error(exc)
                else:
                    self.statusBar().showMessage("Не удалось выполнить действие. Повторите попытку.", 10000)
            else:
                if callback:
                    callback(result)

    def _command(self, operation, message="Сохранено"):
        def done(_):
            self.statusBar().showMessage(message, 6000)
            self.refresh()
        self._submit(operation, done, lambda exc: QMessageBox.warning(
            self, "Действие не выполнено",
            str(exc) if isinstance(exc, ValueError) else "Не удалось выполнить действие. Повторите попытку."))

    def _build(self):
        root = QWidget()
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.setCentralWidget(root)
        sidebar, side = panel("sidebar")
        sidebar.setFixedWidth(213)
        side.addWidget(label("Freelance Bot", "title"))
        self.nav = []
        for index, (text, icon) in enumerate((
            ("Главная", QIcon(str(ASSET_DIR / "home.png"))),
            ("Telegram", QIcon(str(ASSET_DIR / "nav-telegram.png"))),
            ("Настройки", QIcon(str(ASSET_DIR / "settings.png"))),
        )):
            item = button(text, lambda checked=False, i=index: self._navigate(i), "nav", icon)
            item.setCheckable(True)
            side.addWidget(item)
            self.nav.append(item)
        side.addSpacing(16)
        side.addWidget(label("Сервисы", "section"))
        self.parsing_label = button("Парсинг: загрузка…", self._open_parsing_settings, "statusChip")
        self.parsing_label.setProperty("state", "warning")
        side.addWidget(self.parsing_label)
        side.addWidget(label("Автоотклики: не настроено", "chip", "off"))
        side.addWidget(label("Постинг: не настроено", "chip", "off"))
        self.parse_button = button("Управление парсингом", self._open_parsing_settings)
        side.addWidget(self.parse_button)
        side.addStretch()
        side.addWidget(label("В базе", "section"))
        self.metrics = label("Загрузка…", "muted")
        self.metrics.setWordWrap(True)
        side.addWidget(self.metrics)
        layout.addWidget(sidebar)
        self.pages = QStackedWidget()
        layout.addWidget(self.pages, 1)
        self._build_home()
        self._build_sources()
        self._build_settings()
        self._navigate(0)

    def _navigate(self, index):
        self.pages.setCurrentIndex(index)
        for i, item in enumerate(self.nav):
            item.setChecked(i == index)
        self._sync_journal_dock()

    def _build_home(self):
        home = QWidget()
        self.home = home
        outer = QVBoxLayout(home)
        outer.setContentsMargins(14, 12, 12, 8)
        header = QHBoxLayout()
        header.addWidget(label("Рабочая лента", "title"))
        header.addStretch()
        self.top_parsing = button("Парсинг: загрузка…", self._open_parsing_settings, "statusChip")
        self.top_parsing.setProperty("state", "warning")
        header.addWidget(self.top_parsing)
        header.addWidget(label("Автоотклики: не настроено", "chip", "off"))
        self.connection = label("Telegram: загрузка…", "chip", "warning")
        header.addWidget(self.connection)
        outer.addLayout(header)
        self.vertical = QSplitter(Qt.Orientation.Vertical)
        self.workspace = QSplitter(Qt.Orientation.Horizontal)
        center = QWidget()
        center.setMinimumWidth(410)
        feed_layout = QVBoxLayout(center)
        feed_layout.setContentsMargins(0, 0, 0, 0)
        controls = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск по заказам, авторам, ключевым словам…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter_changed)
        controls.addWidget(self.search, 1)
        filters = button("Фильтры", self._toggle_filters)
        controls.addWidget(filters)
        auto = button("Автоматический режим", self._automation_info, "primary", QStyle.StandardPixmap.SP_MediaPlay)
        controls.addWidget(auto)
        feed_layout.addLayout(controls)
        self.filter_panel, filter_layout = panel()
        form = QFormLayout()
        self.source_filter = QComboBox()
        self.source_filter.addItem("Все источники", "")
        self.currency_filter = QComboBox()
        self.currency_filter.addItem("Все валюты", "")
        self.sort_filter = QComboBox()
        for title, key in (("Сначала новые", "newest"), ("Сначала старые", "oldest"),
                           ("Бюджет по убыванию", "budget_desc"), ("Бюджет по возрастанию", "budget_asc"),
                           ("Источник", "source")):
            self.sort_filter.addItem(title, key)
        self.min_budget = QLineEdit()
        self.min_budget.setPlaceholderText("От")
        self.max_budget = QLineEdit()
        self.max_budget.setPlaceholderText("До")
        budget_row = QHBoxLayout()
        budget_row.addWidget(self.min_budget)
        budget_row.addWidget(self.max_budget)
        self.period_filter = QComboBox()
        for title, days in (("За всё время", 0), ("За сутки", 1), ("За неделю", 7), ("За месяц", 30)):
            self.period_filter.addItem(title, days)
        for title, widget in (("Источник", self.source_filter), ("Валюта", self.currency_filter),
                              ("Период", self.period_filter), ("Сортировка", self.sort_filter)):
            form.addRow(title, widget)
            widget.currentIndexChanged.connect(self._filter_changed)
        form.addRow("Бюджет", budget_row)
        self.min_budget.textChanged.connect(self._filter_changed)
        self.max_budget.textChanged.connect(self._filter_changed)
        filter_layout.addLayout(form)
        filter_layout.addWidget(button("Сбросить фильтры", self._reset_filters))
        self.filter_panel.hide()
        feed_layout.addWidget(self.filter_panel)
        self.tabs = QTabBar()
        self.tabs.setExpanding(False)
        for name in self.TAB_NAMES:
            self.tabs.addTab(name)
        self.tabs.currentChanged.connect(self._filter_changed)
        feed_layout.addWidget(self.tabs)
        self.archive_actions = QWidget()
        archive_actions_layout = QHBoxLayout(self.archive_actions)
        archive_actions_layout.setContentsMargins(0, 0, 0, 0)
        archive_actions_layout.setSpacing(6)
        archive_actions_layout.addWidget(button("Выбрать всё", self._select_all_archive_visible))
        archive_actions_layout.addWidget(button("Очистить выбор", self._clear_deletion_selection))
        self.deletion_selection_label = label("Выбрано: 0", "muted")
        archive_actions_layout.addWidget(self.deletion_selection_label)
        archive_actions_layout.addStretch(1)
        self.delete_selected_button = button("Удалить выбранные", self._delete_selected_archive_orders, "primary")
        archive_actions_layout.addWidget(self.delete_selected_button)
        self.cancel_selected_deletions_button = button("Отменить удаление выбранных", self._cancel_selected_deletions)
        archive_actions_layout.addWidget(self.cancel_selected_deletions_button)
        self.archive_actions.hide()
        feed_layout.addWidget(self.archive_actions)
        self.feed_notice = label("Загрузка заказов…", "muted")
        self.feed_notice.setWordWrap(True)
        feed_layout.addWidget(self.feed_notice)
        self.model = FeedModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setItemDelegate(FeedDelegate(self.table))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(39)
        self.table.horizontalHeader().setMinimumSectionSize(65)
        # Keep the reference seven-column composition within the 780 px feed.
        # A wider status column previously forced the order title to collapse.
        for column, width in enumerate((250, 95, 82, 75, 65, 78, 95)):
            self.table.setColumnWidth(column, width)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.clicked.connect(self._select_index)
        self.table.activated.connect(self._select_index)
        self.table.selectionModel().currentRowChanged.connect(self._keyboard_selection)
        self.table.selectionModel().selectionChanged.connect(self._archive_selection_changed)
        feed_layout.addWidget(self.table, 1)
        self.workspace.addWidget(center)
        self._build_details()
        self.workspace.setSizes([766, 470])
        self.workspace.setChildrenCollapsible(False)
        self.vertical.addWidget(self.workspace)
        self.logs_panel, logs_layout = panel()
        log_header = QHBoxLayout()
        log_header.addWidget(label("Журнал событий", "section"))
        log_header.addStretch()
        self.log_filter = QLineEdit()
        self.log_filter.setPlaceholderText("Поиск в журнале")
        self.log_filter.setClearButtonEnabled(True)
        self.log_filter.setMaximumWidth(220)
        self.log_filter.textChanged.connect(self._render_logs)
        log_header.addWidget(self.log_filter)
        self.log_toggle = button("Свернуть", self._toggle_logs)
        log_header.addWidget(self.log_toggle)
        logs_layout.addLayout(log_header)
        self.log_model = EventLogModel(self)
        self.logs = QTableView()
        self.logs.setModel(self.log_model)
        self.logs.setItemDelegate(EventLogDelegate(self.logs))
        self.logs.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.logs.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.logs.setShowGrid(False)
        self.logs.verticalHeader().hide()
        self.logs.verticalHeader().setDefaultSectionSize(24)
        self.logs.horizontalHeader().hide()
        self.logs.horizontalHeader().setMinimumSectionSize(70)
        for column, width in enumerate((96, 90, 360, 90)):
            self.logs.setColumnWidth(column, width)
        self.logs.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.logs.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # The vertical splitter controls the journal height. A widget minimum here would
        # prevent the panel from collapsing to its bottom tab.
        self.logs.setMinimumHeight(0)
        logs_layout.addWidget(self.logs)
        self.logs_empty = label("Событий пока нет", "muted")
        self.logs_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.logs_empty.hide()
        logs_layout.addWidget(self.logs_empty)
        self.vertical.addWidget(self.logs_panel)
        self.log_tab = button("Журнал", self._toggle_logs, "journalTab")
        self.log_tab.setFixedSize(150, 34)
        self.log_tab.setToolTip("Открыть журнал событий")
        self.journal_dock = QFrame()
        self.journal_dock.setObjectName("journalDock")
        self.journal_dock.setFixedHeight(34)
        dock_layout = QHBoxLayout(self.journal_dock)
        dock_layout.setContentsMargins(0, 0, 0, 0)
        dock_layout.setSpacing(0)
        dock_layout.addWidget(self.log_tab, 0, Qt.AlignmentFlag.AlignLeft)
        dock_layout.addStretch()
        self.journal_dock.hide()
        self.vertical.setSizes([785, self.log_height])
        self.vertical.setChildrenCollapsible(False)
        outer.addWidget(self.vertical, 1)
        outer.addWidget(self.journal_dock)
        self.pages.addWidget(home)

    def _build_details(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(385)
        detail, box = panel()
        box.setContentsMargins(12, 8, 12, 8)
        box.setSpacing(5)
        self.detail_title = label("Выберите заказ", "detailTitle")
        self.detail_title.setWordWrap(True)
        box.addWidget(self.detail_title)
        detail_actions = QHBoxLayout()
        self.detail_meta = label("Подробности появятся здесь", "muted")
        self.detail_meta.setWordWrap(True)
        detail_actions.addWidget(self.detail_meta, 1)
        self.open_button = button("Открыть на сайте", self._open_original)
        detail_actions.addWidget(self.open_button)
        self.archive_button = button("В архив", self._archive)
        detail_actions.addWidget(self.archive_button)
        box.addLayout(detail_actions)
        metrics = QHBoxLayout()
        self.detail_metrics = []
        for title in ("Бюджет", "Срок выполнения", "Откликов"):
            frame, inner = panel("metric")
            inner.addWidget(label(title, "muted"))
            value = label("Не указано")
            value.setWordWrap(True)
            inner.addWidget(value)
            metrics.addWidget(frame, 1)
            self.detail_metrics.append(value)
        box.addLayout(metrics)
        box.addWidget(label("Полное описание заказа", "section"))
        self.description = QPlainTextEdit()
        self.description.setReadOnly(True)
        self.description.setMinimumHeight(125)
        self.description.setMaximumHeight(155)
        box.addWidget(self.description)
        ai_header = QHBoxLayout()
        ai_header.addWidget(label("AI-черновик", "section"))
        ai_header.addStretch()
        self.profile_label = label("Профиль: основной", "muted")
        ai_header.addWidget(self.profile_label)
        box.addLayout(ai_header)
        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText("Напишите предложение или создайте черновик…")
        self.editor.setMinimumHeight(140)
        self.editor.textChanged.connect(self._edited)
        box.addWidget(self.editor, 1)
        editor_status = QHBoxLayout()
        self.save_status = label("", "muted")
        self.char_count = label("0 символов", "muted")
        editor_status.addWidget(self.save_status, 1)
        editor_status.addWidget(self.char_count)
        box.addLayout(editor_status)
        self.warning = label("Качество текста: не проверено. AI не настроен — доступен локальный шаблон.", "muted")
        self.warning.setWordWrap(True)
        box.addWidget(self.warning)
        actions = QHBoxLayout()
        self.generate_button = button("Сгенерировать", self._generate)
        self.copy_button = button("Скопировать", self._copy)
        self.clear_button = button("Очистить", self._clear)
        for action in (self.generate_button, self.copy_button, self.clear_button):
            actions.addWidget(action)
        box.addLayout(actions)
        actions = QHBoxLayout()
        self.save_button = button("Сохранить черновик", self._save_current, icon=QStyle.StandardPixmap.SP_DialogSaveButton)
        actions.addWidget(self.save_button)
        review = button("Отправить на проверку", lambda: None, "primary")
        review.setEnabled(False)
        review.setToolTip("Карточки проверки в Telegram ещё не реализованы. Сохраните черновик локально.")
        actions.addWidget(review)
        box.addLayout(actions)
        notice, notice_layout = panel("confirmation")
        warning = label("Перед отправкой проверьте текст", "notice")
        notice_layout.addWidget(warning)
        info = label("Отправка не настроена. Откройте оригинал и скопируйте проверенный текст.", "muted")
        info.setWordWrap(True)
        notice_layout.addWidget(info)
        box.addWidget(notice)
        scroll.setWidget(detail)
        self.workspace.addWidget(scroll)
        self._set_detail_enabled(False)

    def _build_sources(self):
        page, box = panel()
        box.addWidget(label("Telegram", "title"))
        self.telegram_info = label("Подключение проверяется…", "muted")
        self.telegram_info.setWordWrap(True)
        box.addWidget(self.telegram_info)
        tabs = QTabWidget()
        self.telegram_tabs = tabs
        sources = QWidget()
        sources_box = QVBoxLayout(sources)
        sources_box.setContentsMargins(0, 10, 0, 0)
        sources_box.addWidget(label("Источники чтения", "section"))
        row = QHBoxLayout()
        self.source_chat = QLineEdit()
        self.source_chat.setPlaceholderText("@username, ссылка t.me или числовой ID")
        row.addWidget(self.source_chat)
        self.add_source_button = button("+ Добавить источник", self._add_source, "primary")
        self.import_sources_button = button("Импортировать TXT", self._choose_source_import_file)
        row.addWidget(self.add_source_button)
        row.addWidget(self.import_sources_button)
        sources_box.addLayout(row)
        self.sources = QListWidget()
        sources_box.addWidget(self.sources)
        sources_box.addWidget(button("Удалить выбранный источник", self._remove_source))
        info = label("Источники доступны только после проверки прав Bot API или подключённого User API. Бот не вступает в чаты автоматически.", "muted")
        info.setWordWrap(True)
        sources_box.addWidget(info)
        tabs.addTab(sources, "Источники")
        tabs.addTab(self._build_filtering_tab(), "Фильтрация")
        user_api = QWidget()
        user_box = QVBoxLayout(user_api)
        user_box.setContentsMargins(12, 12, 12, 12)
        user_box.addWidget(label("Telegram User API", "section"))
        self.telegram_user_info = label("Состояние подключения проверяется…", "muted")
        self.telegram_user_info.setWordWrap(True)
        user_box.addWidget(self.telegram_user_info)
        user_box.addWidget(button("Открыть подключение User API", self._open_parsing_settings))
        user_box.addWidget(label("Подключение использует ваш Telegram-аккаунт только для доступных ему источников. Telegram-бот используется для ленты и уведомлений.", "muted"))
        user_box.addStretch()
        tabs.addTab(user_api, "User API")
        box.addWidget(tabs, 1)
        self.pages.addWidget(page)

    def _choose_source_import_file(self):
        """Choose a TXT file and hand it to the existing import worker."""
        path, _filter = QFileDialog.getOpenFileName(
            self, "Импорт Telegram-источников", "", "Text files (*.txt)"
        )
        if path:
            self._start_source_import(path)

    def _build_filtering_tab(self):
        """Build the only editable local filtering UI under Telegram."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addWidget(label("Локальная фильтрация", "section"))
        note = label("Каждая фраза вводится с новой строки. Hard Stop пропускает сообщение до создания заказа; Order Signals нужны, чтобы подтвердить запрос на работу.", "muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.include_rules = QPlainTextEdit()
        self.exclude_rules = QPlainTextEdit()
        self.telegram_hard_stops = QPlainTextEdit()
        self.telegram_order_signals = QPlainTextEdit()
        fields = (("Include", self.include_rules), ("Exclude", self.exclude_rules),
                  ("Hard Stop", self.telegram_hard_stops), ("Order Signals", self.telegram_order_signals))
        for title, widget in fields:
            widget.setMaximumHeight(92)
            form.addRow(title, widget)
        layout.addLayout(form)
        layout.addWidget(label("Мои направления", "section"))
        self.category_editors = {}
        self.categories_layout = QVBoxLayout()
        layout.addLayout(self.categories_layout)
        actions = QHBoxLayout()
        actions.addWidget(button("Сохранить правила", self._save_search_profile, "primary"))
        actions.addWidget(button("Восстановить значения по умолчанию", self._restore_default_search_profile))
        actions.addStretch()
        layout.addLayout(actions)
        layout.addStretch()
        scroll.setWidget(content)
        return scroll

    def _build_settings(self):
        page, box = panel()
        box.addWidget(label("Настройки", "title"))
        tabs = QTabWidget()
        accounts, account_box = panel()
        row = QHBoxLayout()
        self.account_name = QLineEdit()
        self.account_name.setPlaceholderText("Название аккаунта")
        self.account_platform = QLineEdit()
        self.account_platform.setPlaceholderText("Площадка")
        row.addWidget(self.account_name)
        row.addWidget(self.account_platform)
        row.addWidget(button("Добавить", self._add_account))
        account_box.addLayout(row)
        self.accounts = QListWidget()
        account_box.addWidget(self.accounts)
        account_box.addWidget(button("Включить / выключить", self._toggle_account))
        account_box.addWidget(label("Интеграции площадок требуют проверки на тестовом аккаунте.", "muted"))
        tabs.addTab(accounts, "Аккаунты")
        profile, profile_box = panel()
        form = QFormLayout()
        self.skills = QLineEdit()
        self.experience = QPlainTextEdit()
        self.style_examples = QPlainTextEdit()
        self.rules = QPlainTextEdit()
        for title, widget in (("Навыки через запятую", self.skills), ("Опыт", self.experience),
                              ("Примеры стиля (разделитель ---)", self.style_examples), ("Правила AI", self.rules)):
            form.addRow(title, widget)
        profile_box.addLayout(form)
        profile_box.addWidget(button("Сохранить профиль", self._save_profile))
        tabs.addTab(profile, "Профиль и AI")
        resources, resource_box = panel()
        self.soft_limit = QSpinBox()
        self.hard_limit = QSpinBox()
        for spin in (self.soft_limit, self.hard_limit):
            spin.setRange(256, 5120)
            spin.setSuffix(" МБ")
        form = QFormLayout()
        form.addRow("Мягкий лимит", self.soft_limit)
        form.addRow("Жёсткий лимит", self.hard_limit)
        resource_box.addLayout(form)
        resource_box.addWidget(button("Применить лимиты", self._save_limits))
        resource_box.addStretch()
        tabs.addTab(resources, "Ресурсы")
        box.addWidget(tabs)
        self.pages.addWidget(page)

    def refresh(self):
        """Request a background snapshot without overlapping reads."""
        if self.snapshot_pending or self.closing:
            return
        self.snapshot_pending = True
        self._submit(self.service.snapshot, self._received, self._refresh_failed)

    def _refresh_failed(self, _error):
        self.snapshot_pending = False
        self.feed_notice.setText("Не удалось загрузить ленту. Нажмите «Обновить».")
        self.feed_notice.show()

    def _received(self, data):
        self.snapshot_pending = False
        self.data = data
        self.model.set_pending_deletions(data.get("pending_deletions", {}))
        self._update_deletion_controls()
        self.connection.setText("Telegram: " + data["telegram"])
        self._set_chip_state(self.connection, "ok" if data["telegram"] == "подключён" else "warning" if data["telegram"] == "подключается" else "off")
        self.parsing_label.setText("Парсинг: " + data["parsing"])
        self.top_parsing.setText("Парсинг: " + data["parsing"])
        parsing_state = "ok" if data["parsing"] == "работает" else "warning" if data["parsing"] == "пауза" else "off"
        self._set_chip_state(self.parsing_label, parsing_state)
        self._set_chip_state(self.top_parsing, parsing_state)
        if hasattr(self, "parsing_dialog"):
            self.parsing_dialog.set_snapshot(data)
        self.metrics.setText(f"Заказов: {data['total']}\nОтправлено: {data['counts']['sent']}\nНа проверке: {data['counts']['review']}")
        for i, name in enumerate(self.TAB_NAMES):
            self.tabs.setTabText(i, f"{name}  {data['counts'][self.GROUPS[i]]}")
        for order_id, text in data["drafts"].items():
            if order_id not in self.dirty:
                self.local_drafts[order_id] = text
        for order_id, job in data["jobs"].items():
            if order_id not in self.local_drafts:
                self.local_drafts[order_id] = job.get("proposal_text", "")
        self._fill_combo(self.source_filter, sorted({str(o.metadata.get("source_name") or o.platform_id) for o, _, _ in data["items"]}), "Все источники")
        self._fill_combo(self.currency_filter, sorted({o.currency for o, _, _ in data["items"] if o.currency}), "Все валюты")
        self._render_feed()
        self._update_archive_button()
        self._render_logs()
        self.telegram_info.setText(f"Бот: {data['telegram']}. Источники требуют проверки доступа.\nТокен и ID владельца задаются в локальном файле .env.")
        self.telegram_user_info.setText("User API: " + data["telegram_user"]["status"] + ". Управление подключением доступно в этом разделе.")
        self._fill_list(self.sources, [(s.chat_id, f"{s.name or s.chat_id} — {'включён' if s.enabled else 'выключен'} · требует проверки") for s in data["sources"]])
        self._fill_list(self.accounts, [(a.id, f"{a.name} · {a.platform_id} · {'включён' if a.enabled else 'выключен'} · требует проверки") for a in data["accounts"]])
        if not self.settings_loaded:
            profile = data["profile"]
            self.skills.setText(", ".join(profile.skills))
            self.experience.setPlainText(profile.experience)
            self.style_examples.setPlainText("\n---\n".join(profile.style_examples))
            self.rules.setPlainText(profile.rules)
            search_profile = data["search_profile"]
            self._set_filtering_profile(search_profile)
            self.soft_limit.setValue(data["limits"][0])
            self.hard_limit.setValue(data["limits"][1])
            self.settings_loaded = True
        self.profile_label.setText("Профиль: " + data["profile"].name)

    def _fill_combo(self, combo, values, title):
        current = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(title, "")
        for value in values:
            combo.addItem(value, value)
        combo.setCurrentIndex(max(0, combo.findData(current)))
        combo.blockSignals(False)

    @staticmethod
    def _set_chip_state(widget, state):
        """Refresh a semantic status color after its runtime state changes."""
        if widget.property("state") == state:
            return
        widget.setProperty("state", state)
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def _fill_list(self, widget, rows):
        selected = widget.currentItem().data(Qt.ItemDataRole.UserRole) if widget.currentItem() else None
        widget.clear()
        for key, text in rows:
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, key)
            widget.addItem(item)
            if key == selected:
                widget.setCurrentItem(item)

    def _filter_changed(self, *_):
        if hasattr(self, "deletion_selection"):
            self.deletion_selection.clear()
        if hasattr(self, "table"):
            self.table.selectionModel().clearSelection()
            is_archive = self.GROUPS[self.tabs.currentIndex()] == "archive"
            self.table.setSelectionMode(
                QAbstractItemView.SelectionMode.ExtendedSelection if is_archive
                else QAbstractItemView.SelectionMode.SingleSelection
            )
            self.archive_actions.setVisible(is_archive)
            self._update_deletion_controls()
        self.page = 0
        if hasattr(self, "table"):
            self._render_feed()

    def _archive_selection_changed(self, *_):
        """Track multi-selection by Order ID, never by transient table row number."""
        if self.GROUPS[self.tabs.currentIndex()] != "archive":
            return
        self.deletion_selection = {
            str(index.data(Qt.ItemDataRole.UserRole))
            for index in self.table.selectionModel().selectedRows(0)
            if index.data(Qt.ItemDataRole.UserRole)
        }
        self._update_deletion_controls()

    def _apply_deletion_selection(self):
        """Restore selected Order IDs after a feed refresh or sort."""
        selection = self.table.selectionModel()
        # Временная очистка Qt-выбора не должна сбросить сохранённые ID заказов.
        selection.blockSignals(True)
        selection.clearSelection()
        for row, ((order, _, _), expanded) in enumerate(self.model.rows):
            if not expanded and order.id in self.deletion_selection:
                selection.select(
                    self.model.index(row, 0),
                    QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
                )
        selection.blockSignals(False)

    def _select_all_archive_visible(self):
        """Select only distinct Order IDs present in the current visible page."""
        if self.GROUPS[self.tabs.currentIndex()] != "archive":
            return
        self.deletion_selection = {order.id for order, _, _ in self.model.items}
        self._apply_deletion_selection()
        self._update_deletion_controls()

    def _clear_deletion_selection(self):
        """Clear the archive selection without affecting any queued request."""
        self.deletion_selection.clear()
        if hasattr(self, "table"):
            selection = self.table.selectionModel()
            selection.blockSignals(True)
            selection.clearSelection()
            selection.blockSignals(False)
        self._update_deletion_controls()

    def _update_deletion_controls(self):
        """Enable only the action that is unambiguous for the current selection."""
        if not hasattr(self, "deletion_selection_label"):
            return
        selected = set(self.deletion_selection)
        pending = set(self.data.get("pending_deletions", {}))
        selected_pending = selected & pending
        self.deletion_selection_label.setText(f"Выбрано: {len(selected)}")
        self.delete_selected_button.setEnabled(bool(selected) and not selected_pending)
        self.cancel_selected_deletions_button.setEnabled(bool(selected) and len(selected_pending) == len(selected))
        if selected_pending and len(selected_pending) != len(selected):
            self.deletion_selection_label.setToolTip("Для действия выберите только обычные архивные заказы или только ожидающие удаления.")
        else:
            self.deletion_selection_label.setToolTip("")

    def _delete_selected_archive_orders(self):
        """Confirm first; only then ask the service to queue the selected archived Orders."""
        order_ids = tuple(self.deletion_selection)
        if not order_ids:
            return
        pending = set(self.data.get("pending_deletions", {}))
        if pending.intersection(order_ids):
            QMessageBox.information(self, "Удаление", "Выберите только архивные заказы без ожидающего удаления.")
            return
        orders = {order.id: order for order, _, _ in self.data.get("items", [])}
        if len(order_ids) == 1:
            order = orders.get(order_ids[0])
            title = (order.title if order else order_ids[0])[:120]
            text = (
                f"Поставить заказ «{title}» на удаление?\n\n"
                "Заказ будет окончательно удалён через 5 минут. До этого момента удаление можно отменить. "
                "После окончательного удаления восстановление через программу будет невозможно."
            )
        else:
            text = (
                f"Поставить на удаление {len(order_ids)} заказов?\n\n"
                "Окончательное удаление произойдёт через 5 минут. До этого момента удаление можно отменить."
            )
        answer = QMessageBox.question(
            self, "Подтверждение удаления", text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        def completed(result):
            self.statusBar().showMessage(
                f"Поставлено на удаление: {result.scheduled}; уже ожидают: {result.already_pending}; "
                f"не в архиве: {result.rejected_not_archived}; не найдены: {result.not_found}", 12000,
            )
            self.refresh()

        self._submit(lambda: self.service.schedule_deletion(order_ids), completed, self._deletion_failed)

    def _cancel_selected_deletions(self):
        """Cancel selected queue entries while leaving their Orders archived."""
        order_ids = tuple(self.deletion_selection)
        pending = set(self.data.get("pending_deletions", {}))
        if not order_ids or not set(order_ids).issubset(pending):
            QMessageBox.information(self, "Отмена удаления", "Выберите только заказы, ожидающие удаления.")
            return

        def completed(amount):
            self.statusBar().showMessage(f"Отменено удалений: {amount}", 6000)
            self._clear_deletion_selection()
            self.refresh()

        self._submit(lambda: self.service.cancel_deletion(order_ids), completed, self._deletion_failed)

    def _deletion_failed(self, _error):
        QMessageBox.warning(self, "Удаление", "Не удалось изменить очередь удаления. Заказы сохранены.")

    def _filtered_items(self):
        items = []
        query = self.search.text().strip().casefold()
        currency = self.currency_filter.currentData()
        source = self.source_filter.currentData()
        days = self.period_filter.currentData() or 0
        sort = self.sort_filter.currentData()
        budget_active = bool(self.min_budget.text() or self.max_budget.text() or sort.startswith("budget"))
        if budget_active and not currency:
            raise ValueError("Выберите валюту для фильтра или сортировки по бюджету.")
        try:
            minimum = float(self.min_budget.text().replace(" ", "").replace(",", ".")) if self.min_budget.text() else None
            maximum = float(self.max_budget.text().replace(" ", "").replace(",", ".")) if self.max_budget.text() else None
        except ValueError:
            raise ValueError("Введите бюджет числом.")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError("Минимальный бюджет не должен превышать максимальный.")
        for item in self.data.get("items", []):
            order, group, _ = item
            source_name = str(order.metadata.get("source_name") or order.platform_id)
            if group != self.GROUPS[self.tabs.currentIndex()]:
                continue
            if source and source != source_name or currency and currency != order.currency:
                continue
            if query and query not in " ".join((order.title, order.description, source_name, author_text(order))).casefold():
                continue
            if minimum is not None and (order.budget is None or order.budget < minimum):
                continue
            if maximum is not None and (order.budget is None or order.budget > maximum):
                continue
            if days and (order.published_at or order.discovered_at).timestamp() < (datetime.now(timezone.utc) - timedelta(days=days)).timestamp():
                continue
            items.append(item)
        if sort.startswith("budget"):
            items.sort(key=lambda item: (item[0].budget is None, (item[0].budget or 0) * (-1 if sort == "budget_desc" else 1)))
        elif sort == "source":
            items.sort(key=lambda item: str(item[0].metadata.get("source_name") or item[0].platform_id))
        else:
            items.sort(key=lambda item: (item[0].published_at or item[0].discovered_at).timestamp(), reverse=sort != "oldest")
        if self.GROUPS[self.tabs.currentIndex()] == "archive":
            pending = set(self.data.get("pending_deletions", {}))
            items.sort(key=lambda item: item[0].id not in pending)
        return items

    def _render_feed(self):
        try:
            items = self._filtered_items()
        except ValueError as exc:
            self.feed_notice.setText(str(exc))
            self.feed_notice.show()
            return
        self.page = min(self.page, max(0, (len(items) - 1) // 50))
        page_items = items[self.page * 50:(self.page + 1) * 50]
        pending_signature = tuple(sorted(
            (order_id, str(row.get("delete_after")), str(row.get("last_error")))
            for order_id, row in self.data.get("pending_deletions", {}).items()
        ))
        signature = (self.selected_id, repr(page_items), self.page, len(items), pending_signature)
        if signature == self.feed_signature:
            self.feed_notice.setVisible(not items)
            return
        self.feed_signature = signature
        scroll = self.table.verticalScrollBar().value()
        self.table.selectionModel().blockSignals(True)
        self.table.clearSpans()
        self.model.replace_items(page_items, self.selected_id)
        visible_ids = {order.id for order, _, _ in page_items}
        self.deletion_selection.intersection_update(visible_ids)
        for row, ((order, _, _), expanded) in enumerate(self.model.rows):
            self.table.setRowHeight(row, 106 if expanded else 37)
            if expanded:
                self.table.setSpan(row, 0, 1, 7)
                self.table.setIndexWidget(self.model.index(row, 0), self._expanded_row(order))
            elif order.id == self.selected_id:
                self.table.setCurrentIndex(self.model.index(row, 0))
        if self.GROUPS[self.tabs.currentIndex()] == "archive":
            self._apply_deletion_selection()
        self.table.selectionModel().blockSignals(False)
        self.table.verticalScrollBar().setValue(scroll)
        self.feed_notice.setText("Ничего не найдено. Измените фильтры или добавьте Telegram-источник." if not items else "")
        self.feed_notice.setVisible(not items)

    def _expanded_row(self, order):
        frame, box = panel("expanded")
        box.setContentsMargins(14, 5, 12, 7)
        box.setSpacing(6)
        summary = label(" ".join(order.description.split()), "muted")
        summary.setWordWrap(True)
        summary.setMaximumHeight(34)
        box.addWidget(summary)
        row = QHBoxLayout()
        row.setSpacing(6)
        tags = order.metadata.get("tags", [])
        if isinstance(tags, str):
            tags = [tags]
        for item in tags[:4]:
            row.addWidget(label(str(item), "feedTag"))
        row.addStretch(1)
        row.addWidget(button("Открыть", self._open_original))
        row.addWidget(button("Написать вручную", lambda: self.editor.setFocus()))
        row.addWidget(button("AI-черновик", self._generate))
        box.addLayout(row)
        return frame

    def _keyboard_selection(self, current, _previous):
        if current.isValid():
            order_id = current.data(Qt.ItemDataRole.UserRole)
            QTimer.singleShot(0, lambda key=order_id: self.select_order(key))

    def _select_index(self, index):
        if not index.isValid() or index.row() >= len(self.model.rows):
            return
        self.select_order(index.data(Qt.ItemDataRole.UserRole))

    def select_order(self, order_id):
        """Select by stable ID and save the previous editor before switching."""
        if order_id == self.selected_id:
            return
        self._save_current()
        self.selected_id = order_id
        self._show_details()
        self._render_feed()

    def _selected_order(self):
        return next((o for o, _, _ in self.data.get("items", []) if o.id == self.selected_id), None)

    def _show_details(self):
        order = self._selected_order()
        self._set_detail_enabled(order is not None)
        if order is None:
            return
        self.detail_title.setText(order.title)
        published = order.published_at or order.discovered_at
        stamp_label = "Опубликован" if order.published_at else "Обнаружен"
        self.detail_meta.setText(
            f"{order.metadata.get('source_name') or order.platform_id}  ·  {author_text(order)}\n"
            f"{stamp_label}: {published.astimezone().strftime('%d.%m.%Y %H:%M')}"
        )
        self.detail_metrics[0].setText(budget_text(order))
        self.detail_metrics[1].setText(str(order.metadata.get("deadline") or "Не указано"))
        self.detail_metrics[2].setText(str(order.metadata.get("responses", "Не указано")))
        self.description.setPlainText(order.description)
        self.editor.blockSignals(True)
        self.editor.setPlainText(self.local_drafts.get(order.id, ""))
        self.editor.blockSignals(False)
        self.char_count.setText(f"{len(self.editor.toPlainText())} символов")
        self.save_status.setText("Не сохранено" if order.id in self.dirty else "Сохранено" if self.editor.toPlainText() else "")
        self._update_archive_button()

    def _update_archive_button(self):
        """Keep the detail archive action aligned with the durable deletion queue."""
        order = self._selected_order()
        pending = bool(order and order.id in self.data.get("pending_deletions", {}))
        if order is not None:
            self.archive_button.setText(
                "Ожидает удаления" if pending else
                "Восстановить из архива" if order.status.value != "active" else "В архив"
            )
        self.archive_button.setEnabled(order is not None and not pending)

    def _set_detail_enabled(self, enabled):
        for widget in (self.editor, self.open_button, self.generate_button, self.copy_button,
                       self.save_button, self.clear_button, self.archive_button):
            widget.setEnabled(enabled)

    def _edited(self):
        if self.selected_id is None:
            return
        self.local_drafts[self.selected_id] = self.editor.toPlainText()
        self.dirty.add(self.selected_id)
        self.versions[self.selected_id] = self.versions.get(self.selected_id, 0) + 1
        self.char_count.setText(f"{len(self.editor.toPlainText())} символов")
        self.save_status.setText("Не сохранено")
        self.save_timer.start()

    def _save_current(self):
        self.save_timer.stop()
        if self.selected_id in self.dirty:
            self._save_id(self.selected_id)

    def _save_id(self, order_id):
        text = self.local_drafts[order_id]
        version = self.versions.get(order_id, 0)
        if order_id == self.selected_id:
            self.save_status.setText("Сохранение…")
        def done(_):
            if self.versions.get(order_id, 0) == version:
                self.dirty.discard(order_id)
                if self.selected_id == order_id:
                    self.save_status.setText("Сохранено")
        def failed(_):
            if order_id == self.selected_id:
                self.save_status.setText("Ошибка сохранения — повторите")
        self._submit(lambda: self.service.save_draft(order_id, text), done, failed)

    def _generate(self):
        order_id = self.selected_id
        if order_id is None:
            return
        if self.editor.toPlainText().strip() and QMessageBox.question(self, "Заменить черновик", "Заменить текущий текст новым черновиком?") != QMessageBox.StandardButton.Yes:
            return
        version = self.versions.get(order_id, 0)
        self.generate_button.setEnabled(False)
        def done(result):
            self.generate_button.setEnabled(self.selected_id is not None)
            # A delayed generator result must never overwrite subsequent typing.
            if self.versions.get(order_id, 0) != version:
                self.statusBar().showMessage("Текст изменён во время генерации. Ваши правки сохранены.", 8000)
                return
            self.local_drafts[order_id] = result.text
            self.dirty.add(order_id)
            self.versions[order_id] = version + 1
            if self.selected_id == order_id:
                self.editor.setPlainText(result.text)
                self.warning.setText("\n".join(result.warnings + result.questions))
            self._save_id(order_id)
        def failed(_):
            self.generate_button.setEnabled(self.selected_id is not None)
            self.warning.setText("Не удалось создать черновик. Повторите попытку.")
        self._submit(lambda: self.service.generate(order_id), done, failed)

    def _copy(self):
        self.qt_app.clipboard().setText(self.editor.toPlainText())
        self.statusBar().showMessage("Черновик скопирован", 4000)

    def _clear(self):
        if self.editor.toPlainText() and QMessageBox.question(self, "Очистить черновик", "Удалить сохранённый текст черновика?") == QMessageBox.StandardButton.Yes:
            self.editor.clear()
            self._save_current()

    def _open_original(self):
        if self.selected_id:
            order_id = self.selected_id
            self._command(lambda: self.service.open_original(order_id), "Оригинал открыт")

    def _archive(self):
        order = self._selected_order()
        if order:
            self._save_current()
            self._command(lambda: self.service.archive(order.id, order.status.value != "active"), "Статус заказа изменён")

    def _change_page(self, delta):
        self.page = max(0, self.page + delta)
        self._render_feed()

    def _toggle_filters(self):
        self.filter_panel.setVisible(not self.filter_panel.isVisible())

    def _reset_filters(self):
        for combo in (self.source_filter, self.currency_filter, self.period_filter, self.sort_filter):
            combo.setCurrentIndex(0)
        self.min_budget.clear()
        self.max_budget.clear()
        self.search.clear()

    def _automation_info(self):
        QMessageBox.information(self, "Автоматический режим", "Автоотклики не настроены. Адаптеры отправки требуют реализации и проверки на тестовом аккаунте.")

    def _open_parsing_settings(self):
        """Open the non-blocking parsing control center and keep the feed usable."""
        if not hasattr(self, "parsing_dialog"):
            self.parsing_dialog = ParsingSettingsDialog(self)
            self.parsing_dialog.toggle_requested.connect(
                lambda running: self._parsing_dialog_command(
                    lambda: self.service.toggle_parsing(running),
                    "Поиск запущен." if running else "Поиск поставлен на паузу.",
                )
            )
            self.parsing_dialog.autostart_requested.connect(
                lambda enabled: self._parsing_dialog_command(
                    lambda: self.service.set_parsing_autostart(enabled),
                    "Автозапуск парсинга включён." if enabled else "Автозапуск парсинга выключен.",
                )
            )
            self.parsing_dialog.add_marketplace_requested.connect(
                lambda name, url: self._parsing_dialog_command(
                    lambda: self.service.add_marketplace(name, url), "Сайт добавлен в таблицу."
                )
            )
            self.parsing_dialog.marketplace_toggle_requested.connect(
                lambda marketplace_id, enabled: self._parsing_dialog_command(
                    lambda: self.service.set_marketplace_enabled(marketplace_id, enabled),
                    "Состояние сайта сохранено.",
                )
            )
            self.parsing_dialog.remove_marketplace_requested.connect(
                lambda marketplace_id: self._parsing_dialog_command(
                    lambda: self.service.remove_marketplace(marketplace_id), "Сайт удалён."
                )
            )
            self.parsing_dialog.add_source_requested.connect(
                lambda source_id, name, enabled, language, interval: self._parsing_dialog_command(
                    lambda: self.service.add_source(source_id, name, enabled, language, interval),
                    "Telegram-источник добавлен.",
                )
            )
            self.parsing_dialog.import_sources_requested.connect(self._start_source_import)
            self.parsing_dialog.import_cancel_requested.connect(self._cancel_source_import)
            self.parsing_dialog.source_toggle_requested.connect(
                lambda source_id, enabled: self._parsing_dialog_command(
                    lambda: self.service.set_source_enabled(source_id, enabled),
                    "Состояние Telegram-источника сохранено.",
                )
            )
            self.parsing_dialog.update_source_requested.connect(
                lambda source_id, name, enabled, language, interval: self._parsing_dialog_command(
                    lambda: self.service.update_source(source_id, name, enabled, language, interval),
                    "Настройки источника сохранены.",
                )
            )
            self.parsing_dialog.remove_source_requested.connect(
                lambda source_id: self._parsing_dialog_command(
                    lambda: self.service.remove_source(source_id), "Источник удалён."
                )
            )
            self.parsing_dialog.user_login_requested.connect(
                lambda api_id, api_hash, phone: self._begin_telegram_user_login(api_id, api_hash, phone)
            )
            self.parsing_dialog.user_code_requested.connect(
                lambda challenge, code: self._complete_telegram_user_code(challenge, code)
            )
            self.parsing_dialog.user_password_requested.connect(
                lambda challenge, password: self._complete_telegram_user_password(challenge, password)
            )
            self.parsing_dialog.user_disconnect_requested.connect(
                lambda forget: self._parsing_dialog_command(
                    lambda: self.service.disconnect_telegram_user(forget),
                    "Подключение Telegram User API удалено." if forget else "Telegram User API отключён.",
                )
            )
            self.parsing_dialog.refresh_requested.connect(self.refresh)
        self.parsing_dialog.set_snapshot(self.data)
        self.parsing_dialog.show()
        self.parsing_dialog.raise_()
        self.parsing_dialog.activateWindow()

    def _start_source_import(self, path):
        """Run TXT import on the existing desktop worker, never in the Qt thread."""
        if self._source_import_progress is not None:
            if hasattr(self, "parsing_dialog"):
                self.parsing_dialog.show_feedback("Импорт уже выполняется.", True)
            if self._source_import_dialog is not None:
                self._source_import_dialog.raise_()
                self._source_import_dialog.activateWindow()
            return
        progress = ImportProgress()
        cancel = Event()
        self._source_import_progress = progress
        self._source_import_cancel = cancel
        self._source_import_dialog = SourceImportProgressDialog(self)
        self._source_import_dialog.cancel_requested.connect(self._cancel_source_import)
        self._source_import_dialog.show()
        if hasattr(self, "parsing_dialog"):
            self.parsing_dialog.set_import_running(True)
            self.parsing_dialog.show_feedback("Импорт списка выполняется…")

        def done(result):
            self._source_import_progress = None
            self._source_import_cancel = None
            self._source_import_dialog.finish(result)
            if hasattr(self, "parsing_dialog"):
                self.parsing_dialog.set_import_running(False)
            state = "Импорт отменён" if result.cancelled else "Импорт завершён"
            if hasattr(self, "parsing_dialog"):
                self.parsing_dialog.show_feedback(
                    f"{state}. Обработано {result.processed} из {result.total}; добавлено {result.added}, "
                    f"уже было {result.duplicate}, ошибок {result.errors}."
                )
            self.refresh()

        def failed(error):
            self._source_import_progress = None
            self._source_import_cancel = None
            message = str(error) if isinstance(error, ValueError) else "Не удалось прочитать TXT-файл."
            self._source_import_dialog.fail(message)
            if hasattr(self, "parsing_dialog"):
                self.parsing_dialog.set_import_running(False)
                self.parsing_dialog.show_feedback(message, True)

        self._submit(lambda: self.service.import_telegram_sources(path, progress, cancel), done, failed)

    def _cancel_source_import(self):
        """Stop scheduling new rows while allowing the current safe call to finish."""
        if self._source_import_cancel is not None:
            self._source_import_cancel.set()
            if hasattr(self, "parsing_dialog"):
                self.parsing_dialog.show_feedback("Отмена запрошена. Текущая операция завершается…")
            if self._source_import_dialog is not None:
                self._source_import_dialog.status.setText("Отмена запрошена. Текущая операция завершается…")

    def _parsing_dialog_command(self, operation, success_message):
        """Run a parsing command in the worker and report its result inside the dialog."""
        def done(_result):
            self.parsing_dialog.show_feedback(success_message)
            self.refresh()

        def failed(error):
            message = str(error) if isinstance(error, ValueError) else "Не удалось выполнить действие. Повторите попытку."
            self.parsing_dialog.show_feedback(message, True)

        self._submit(operation, done, failed)

    def _begin_telegram_user_login(self, api_id, api_hash, phone):
        def done(challenge):
            self.parsing_dialog.set_user_challenge(challenge)
            self.parsing_dialog.show_feedback("Введите код, полученный в Telegram.")

        self._submit(lambda: self.service.begin_telegram_user_login(api_id, api_hash, phone), done,
                     lambda error: self.parsing_dialog.show_feedback(str(error) if isinstance(error, ValueError) else "Не удалось запросить код.", True))

    def _complete_telegram_user_code(self, challenge, code):
        def done(result):
            if result == "password_required":
                self.parsing_dialog.set_user_password_required()
            else:
                self.parsing_dialog.complete_user_login()
                self.refresh()

        self._submit(lambda: self.service.complete_telegram_user_code(challenge, code), done,
                     lambda error: self.parsing_dialog.show_feedback(str(error) if isinstance(error, ValueError) else "Код не принят.", True))

    def _complete_telegram_user_password(self, challenge, password):
        def done(_result):
            self.parsing_dialog.complete_user_login()
            self.refresh()

        self._submit(lambda: self.service.complete_telegram_user_password(challenge, password), done,
                     lambda error: self.parsing_dialog.show_feedback(str(error) if isinstance(error, ValueError) else "Пароль не принят.", True))

    def _toggle_parsing(self):
        running = not self.data.get("running", False)
        self._command(lambda: self.service.toggle_parsing(running), "Состояние поиска обновлено")

    def _render_logs(self):
        events = self.data.get("logs", [])
        query = self.log_filter.text().casefold().strip()
        visible = []
        for event in events:
            searchable = " ".join(str(event.get(key) or "") for key in ("status", "action", "service", "operation_id"))
            if query and query not in searchable.casefold():
                continue
            visible.append(event)
        signature = tuple(
            (event.get("created_at"), event.get("status"), event.get("action"), event.get("service"), event.get("operation_id"))
            for event in visible
        )
        if signature == self.log_signature:
            return
        self.log_signature = signature
        scroll = self.logs.verticalScrollBar().value()
        self.log_model.replace_items(visible)
        self.logs.verticalScrollBar().setValue(scroll)
        self.logs.setVisible(bool(visible) and self.logs_expanded)
        self.logs_empty.setVisible(not visible and self.logs_expanded)

    def _toggle_logs(self):
        if self.logs_expanded:
            current_sizes = self.vertical.sizes()
            if len(current_sizes) == 2 and current_sizes[1] > 0:
                self.log_height = current_sizes[1]
            self.logs_expanded = False
            self.logs_panel.hide()
            self._sync_journal_dock()
            self.log_toggle.setText("Развернуть")
            return
        self.logs_expanded = True
        self._sync_journal_dock()
        self.logs_panel.show()
        self._resize_log_splitter(self.log_height)
        self.logs.setVisible(bool(self.log_model.items))
        self.logs_empty.setVisible(not self.log_model.items)
        self.log_toggle.setText("Свернуть")

    def _resize_log_splitter(self, log_height):
        """Resize only the vertical journal slot and preserve horizontal feed geometry."""
        total = self.vertical.height()
        if total <= 0:
            return
        target = max(1, min(int(log_height), total - 1))
        self.vertical.setSizes([total - target, target])

    def _sync_journal_dock(self):
        """Show the collapsed journal tab in its own bottom-left dock, never above content."""
        if not hasattr(self, "journal_dock"):
            return
        visible = not self.logs_expanded and self.pages.currentWidget() is self.home
        self.journal_dock.setVisible(visible)
        self.log_tab.setVisible(visible)

    def _add_source(self):
        value = self.source_chat.text()
        self._command(lambda: self.service.add_source(value), "Источник добавлен")

    def _remove_source(self):
        if self.sources.currentItem():
            key = self.sources.currentItem().data(Qt.ItemDataRole.UserRole)
            self._command(lambda: self.service.remove_source(key), "Источник удалён")

    def _add_account(self):
        name, platform = self.account_name.text(), self.account_platform.text()
        self._command(lambda: self.service.add_account(name, platform), "Аккаунт добавлен; требуется проверка")

    def _toggle_account(self):
        if self.accounts.currentItem():
            key = self.accounts.currentItem().data(Qt.ItemDataRole.UserRole)
            self._command(lambda: self.service.toggle_account(key))

    def _save_profile(self):
        values = (self.skills.text(), self.experience.toPlainText(), self.style_examples.toPlainText(), self.rules.toPlainText())
        self._command(lambda: self.service.save_profile(*values), "Профиль сохранён")

    def _save_search_profile(self):
        categories = []
        for name, (enabled, editor) in self.category_editors.items():
            ordinary, important = [], []
            for rule in editor.toPlainText().splitlines():
                rule = rule.strip()
                if rule.startswith("!"):
                    important.append(rule[1:].strip())
                elif rule:
                    ordinary.append(rule)
            categories.append({"name": name, "enabled": enabled.isChecked(), "rules": ordinary,
                               "important_rules": important})
        self._command(
            lambda: self.service.save_search_profile(
                self.include_rules.toPlainText(), self.exclude_rules.toPlainText(),
                service_categories=categories,
                telegram_hard_stops=self.telegram_hard_stops.toPlainText(),
                telegram_order_signals=self.telegram_order_signals.toPlainText(),
            ),
            "Правила фильтрации сохранены; они применяются к новым заказам.",
        )

    def _restore_default_search_profile(self):
        self.settings_loaded = False
        self._command(self.service.restore_default_search_profile, "Значения фильтрации по умолчанию восстановлены.")

    def _set_filtering_profile(self, profile):
        """Populate the Telegram filtering widgets from the one persisted profile."""
        self.include_rules.setPlainText("\n".join(profile.include_keywords))
        self.exclude_rules.setPlainText("\n".join(profile.exclude_keywords))
        self.telegram_hard_stops.setPlainText("\n".join(profile.telegram_hard_stops))
        self.telegram_order_signals.setPlainText("\n".join(profile.telegram_order_signals))
        while self.categories_layout.count():
            child = self.categories_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        self.category_editors = {}
        for category in profile.service_categories:
            row, row_layout = panel()
            enabled = QCheckBox(category.name)
            enabled.setChecked(category.enabled)
            editor = QPlainTextEdit()
            editor.setMaximumHeight(78)
            editor.setPlaceholderText("Обычное правило\n!Важное правило")
            editor.setPlainText("\n".join(list(category.rules) + [f"!{rule}" for rule in category.important_rules]))
            row_layout.addWidget(enabled)
            row_layout.addWidget(editor)
            self.categories_layout.addWidget(row)
            self.category_editors[category.name] = (enabled, editor)

    def _save_limits(self):
        soft, hard = self.soft_limit.value(), self.hard_limit.value()
        self._command(lambda: self.service.save_limits(soft, hard), "Лимиты сохранены")

    def closeEvent(self, event):
        """Drain edits before closing; failed writes require an explicit choice."""
        if self.closed:
            event.accept()
            return
        event.ignore()
        if self.closing:
            return
        self.closing = True
        self.refresh_timer.stop()
        self.archive_timer.stop()
        self.deletion_countdown_timer.stop()
        self.save_timer.stop()
        self.centralWidget().setEnabled(False)
        for order_id in list(self.dirty):
            self._save_id(order_id)
        self._submit(lambda: None, self._finish_close)

    def _finish_close(self, _):
        if self.dirty:
            choice = QMessageBox.warning(self, "Черновик не сохранён", "Не удалось сохранить изменения.",
                QMessageBox.StandardButton.Retry | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel)
            self.closing = False
            self.centralWidget().setEnabled(True)
            if choice == QMessageBox.StandardButton.Retry:
                self.close()
                return
            if choice == QMessageBox.StandardButton.Cancel:
                self.refresh_timer.start()
                return
        self.closing = True
        self._submit(self.service.shutdown, self._closed, self._shutdown_failed)

    def _shutdown_failed(self, _):
        self.closing = False
        self.centralWidget().setEnabled(True)
        QMessageBox.warning(self, "Завершение", "Не удалось завершить сервисы. Повторите закрытие.")

    def _closed(self, _):
        self.closed = True
        self.poll_timer.stop()
        self.executor.shutdown(wait=False)
        self.close()

    def run(self):
        """Show the native window and enter Qt's event loop."""
        self.show()
        self.qt_app.exec()
