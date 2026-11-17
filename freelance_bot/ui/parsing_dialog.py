"""Table-based parsing control center for the PySide6 dashboard."""

from functools import partial

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFrame, QHBoxLayout,
    QFileDialog, QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox,
    QStackedWidget, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)


def _plain_label(text, name=None):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if name:
        widget.setObjectName(name)
    return widget


def _panel():
    frame = QFrame()
    frame.setObjectName("panel")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(12, 12, 12, 12)
    layout.setSpacing(10)
    return frame, layout


class SourceToggleButton(QPushButton):
    """A checkable lamp button for an independent source state."""

    def __init__(self, enabled=False, parent=None):
        super().__init__(parent)
        self.setObjectName("sourceToggle")
        self.setCheckable(True)
        self.setFixedSize(26, 26)
        self.setChecked(enabled)
        self.toggled.connect(self._update_presentation)
        self._update_presentation(enabled)

    def _update_presentation(self, enabled):
        self.setText("")
        self.setProperty("enabledState", "on" if enabled else "off")
        self.setAccessibleName("Источник: включено" if enabled else "Источник: выключено")
        self.setToolTip("Зелёный: источник включён. Нажмите, чтобы выключить." if enabled else "Красный: источник выключен. Нажмите, чтобы включить.")
        self.style().unpolish(self)
        self.style().polish(self)


class ParsingSettingsDialog(QDialog):
    """Manage parsing runtime, marketplace targets, and Telegram sources in tables."""

    toggle_requested = Signal(bool)
    autostart_requested = Signal(bool)
    add_marketplace_requested = Signal(str, str)
    marketplace_toggle_requested = Signal(str, bool)
    remove_marketplace_requested = Signal(str)
    add_source_requested = Signal(str, str, bool, str, int)
    import_sources_requested = Signal(str)
    import_cancel_requested = Signal()
    source_toggle_requested = Signal(str, bool)
    update_source_requested = Signal(str, str, bool, str, int)
    remove_source_requested = Signal(str)
    user_login_requested = Signal(str, str, str)
    user_code_requested = Signal(str, str)
    user_password_requested = Signal(str, str)
    user_disconnect_requested = Signal(bool)
    refresh_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.running = False
        self._updating = False
        self._source_edits = {}
        self._user_challenge = ""
        self.setWindowTitle("Настройки парсинга")
        self.setModal(False)
        self.resize(1040, 690)
        self.setMinimumSize(900, 600)
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 16)
        root.setSpacing(12)
        title_row = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(3)
        title_box.addWidget(_plain_label("Настройки парсинга", "dialogTitle"))
        title_box.addWidget(_plain_label("Управление биржами, сайтами и Telegram-источниками", "muted"))
        title_row.addLayout(title_box, 1)
        self.runtime_status = _plain_label("Загрузка…", "dialogStatus")
        title_row.addWidget(self.runtime_status)
        root.addLayout(title_row)

        metrics = QHBoxLayout()
        self.connection_card = self._metric("Telegram", "Проверяется…")
        self.marketplaces_card = self._metric("Биржи и сайты", "0 включено")
        self.sources_card = self._metric("Telegram-источники", "0 активно")
        for card, _value in (self.connection_card, self.marketplaces_card, self.sources_card):
            metrics.addWidget(card)
        root.addLayout(metrics)

        controls = QHBoxLayout()
        self.toggle_button = QPushButton("Запустить парсинг")
        self.toggle_button.setObjectName("primary")
        self.toggle_button.clicked.connect(self._request_toggle)
        controls.addWidget(self.toggle_button)
        refresh = QPushButton("Обновить статусы")
        refresh.clicked.connect(self.refresh_requested.emit)
        controls.addWidget(refresh)
        controls.addStretch()
        self.autostart = QCheckBox("Запускать парсинг при старте программы")
        self.autostart.toggled.connect(self._autostart_changed)
        controls.addWidget(self.autostart)
        root.addLayout(controls)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_marketplaces_tab(), "Биржи и сайты")
        self.tabs.addTab(self._build_telegram_tab(), "Telegram")
        root.addWidget(self.tabs, 1)
        self.feedback = _plain_label("", "muted")
        root.addWidget(self.feedback)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("Закрыть")
        buttons.rejected.connect(self.close)
        root.addWidget(buttons)

    def _build_marketplaces_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 10, 0, 0)
        self.marketplaces_table = self._table(("Источник", "Биржа / сайт", "Адрес", "Статус", "Состояние"))
        header = self.marketplaces_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.marketplaces_table.setColumnWidth(0, 92)
        self.marketplaces_table.setColumnWidth(3, 112)
        self.marketplaces_empty = _plain_label("Пока нет сайтов для парсинга. Добавьте первый источник ниже.", "emptyState")
        self.marketplaces_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.marketplaces_area = self._table_area(self.marketplaces_table, self.marketplaces_empty)
        layout.addWidget(self.marketplaces_area, 1)
        add_panel, add_layout = _panel()
        add_layout.addWidget(_plain_label("Добавить сайт для парсинга", "section"))
        row = QHBoxLayout()
        self.marketplace_name = QLineEdit()
        self.marketplace_name.setPlaceholderText("Название биржи или сайта")
        self.marketplace_url = QLineEdit()
        self.marketplace_url.setPlaceholderText("https://example.com")
        self.marketplace_url.returnPressed.connect(self._add_marketplace)
        add = QPushButton("Добавить в таблицу")
        add.setObjectName("primary")
        add.clicked.connect(self._add_marketplace)
        row.addWidget(self.marketplace_name, 1)
        row.addWidget(self.marketplace_url, 2)
        row.addWidget(add)
        add_layout.addLayout(row)
        note = _plain_label(
            "Включение сохраняет источник. Реальный сбор начнётся только после подключения и проверки отдельного адаптера площадки.",
            "dialogNotice",
        )
        note.setWordWrap(True)
        add_layout.addWidget(note)
        layout.addWidget(add_panel)
        return tab

    def _build_telegram_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 10, 0, 0)
        auth_panel, auth_layout = _panel()
        auth_layout.addWidget(_plain_label("Telegram User API", "section"))
        self.user_status = _plain_label("Не настроен", "dialogStatus")
        auth_layout.addWidget(self.user_status)
        self.user_api_id = QLineEdit()
        self.user_api_id.setPlaceholderText("API ID из my.telegram.org")
        self.user_api_hash = QLineEdit()
        self.user_api_hash.setPlaceholderText("API Hash")
        self.user_api_hash.setEchoMode(QLineEdit.EchoMode.Password)
        self.user_phone = QLineEdit()
        self.user_phone.setPlaceholderText("Номер телефона в международном формате")
        credentials = QHBoxLayout()
        credentials.addWidget(self.user_api_id)
        credentials.addWidget(self.user_api_hash, 2)
        credentials.addWidget(self.user_phone)
        self.user_connect = QPushButton("Подключить аккаунт")
        self.user_connect.setObjectName("primary")
        self.user_connect.clicked.connect(self._begin_user_login)
        credentials.addWidget(self.user_connect)
        auth_layout.addLayout(credentials)
        self.user_code = QLineEdit()
        self.user_code.setPlaceholderText("Код подтверждения")
        self.user_code.setEchoMode(QLineEdit.EchoMode.Password)
        self.user_code.returnPressed.connect(self._submit_user_code)
        self.user_code_button = QPushButton("Подтвердить код")
        self.user_code_button.clicked.connect(self._submit_user_code)
        self.user_password = QLineEdit()
        self.user_password.setPlaceholderText("Пароль 2FA")
        self.user_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.user_password.returnPressed.connect(self._submit_user_password)
        self.user_password_button = QPushButton("Подтвердить 2FA")
        self.user_password_button.clicked.connect(self._submit_user_password)
        self.user_code_row = QHBoxLayout()
        self.user_code_row.addWidget(self.user_code)
        self.user_code_row.addWidget(self.user_code_button)
        self.user_password_row = QHBoxLayout()
        self.user_password_row.addWidget(self.user_password)
        self.user_password_row.addWidget(self.user_password_button)
        self._user_code_holder = QWidget()
        self._user_code_holder.setLayout(self.user_code_row)
        self._user_password_holder = QWidget()
        self._user_password_holder.setLayout(self.user_password_row)
        self._user_code_holder.hide()
        self._user_password_holder.hide()
        auth_layout.addWidget(self._user_code_holder)
        auth_layout.addWidget(self._user_password_holder)
        disconnect = QHBoxLayout()
        self.user_disconnect = QPushButton("Отключить")
        self.user_disconnect.clicked.connect(lambda: self.user_disconnect_requested.emit(False))
        self.user_forget = QPushButton("Выйти и удалить подключение")
        self.user_forget.setObjectName("danger")
        self.user_forget.clicked.connect(self._forget_user)
        disconnect.addWidget(self.user_disconnect)
        disconnect.addWidget(self.user_forget)
        disconnect.addStretch()
        auth_layout.addLayout(disconnect)
        notice = _plain_label("Подключается ваш Telegram-аккаунт. Коды, 2FA-пароль и сессия не показываются и не сохраняются в интерфейсе.", "dialogNotice")
        notice.setWordWrap(True)
        auth_layout.addWidget(notice)
        layout.addWidget(auth_panel)
        self.telegram_table = self._table(("Источник", "Название", "ID канала / чата", "Язык", "Интервал", "Статус", "Действия"))
        header = self.telegram_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for column in (3, 4, 5, 6):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
        self.telegram_table.setColumnWidth(0, 92)
        self.telegram_table.setColumnWidth(3, 90)
        self.telegram_table.setColumnWidth(4, 108)
        self.telegram_table.setColumnWidth(5, 100)
        self.telegram_table.setColumnWidth(6, 200)
        self.telegram_empty = _plain_label("Пока нет Telegram-каналов или чатов. Добавьте источник ниже.", "emptyState")
        self.telegram_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.telegram_area = self._table_area(self.telegram_table, self.telegram_empty)
        layout.addWidget(self.telegram_area, 1)
        add_panel, add_layout = _panel()
        add_layout.addWidget(_plain_label("Добавить Telegram-источник", "section"))
        first = QHBoxLayout()
        self.new_source_id = QLineEdit()
        self.new_source_id.setPlaceholderText("@username, ссылка t.me или числовой ID")
        self.new_source_name = QLineEdit()
        self.new_source_name.setPlaceholderText("Название канала или чата")
        first.addWidget(self.new_source_id, 1)
        first.addWidget(self.new_source_name, 1)
        add_layout.addLayout(first)
        second = QHBoxLayout()
        self.new_source_language = QComboBox()
        self.new_source_language.addItem("Русский", "ru")
        self.new_source_language.addItem("English", "en")
        self.new_source_interval = QSpinBox()
        self.new_source_interval.setRange(30, 86400)
        self.new_source_interval.setValue(120)
        self.new_source_interval.setSuffix(" сек")
        self.new_source_interval.setSingleStep(30)
        add = QPushButton("Добавить источник")
        add.setObjectName("primary")
        add.clicked.connect(self._add_source)
        second.addWidget(_plain_label("Язык", "muted"))
        second.addWidget(self.new_source_language)
        second.addWidget(_plain_label("Интервал", "muted"))
        second.addWidget(self.new_source_interval)
        second.addStretch()
        second.addWidget(add)
        import_button = QPushButton("Импортировать TXT")
        import_button.clicked.connect(self._choose_import_file)
        second.addWidget(import_button)
        add_layout.addLayout(second)
        import_row = QHBoxLayout()
        self.import_progress_label = _plain_label("", "muted")
        self.import_cancel_button = QPushButton("Отменить импорт")
        self.import_cancel_button.setVisible(False)
        self.import_cancel_button.clicked.connect(self.import_cancel_requested.emit)
        import_row.addWidget(self.import_progress_label, 1)
        import_row.addWidget(self.import_cancel_button)
        add_layout.addLayout(import_row)
        layout.addWidget(add_panel)
        return tab

    def _choose_import_file(self):
        """Choose a local UTF-8 TXT file in the Qt thread and start its worker."""
        path, _filter = QFileDialog.getOpenFileName(self, "Импорт Telegram-источников", "", "Text files (*.txt)")
        if path:
            self.import_sources_requested.emit(path)

    def set_import_progress(self, values):
        """Render worker progress without touching widgets from the worker."""
        total = int(values.get("total", 0))
        processed = int(values.get("processed", 0))
        self.import_progress_label.setText(
            f"Обработано: {processed} / {total} · Добавлено: {int(values.get('added', 0))} · "
            f"Уже существует: {int(values.get('duplicate', 0))} · "
            f"Нет доступа: {int(values.get('no_access', 0))} · "
            f"Некорректных: {int(values.get('invalid', 0))} · Ошибок: {int(values.get('errors', 0))}"
        )

    def set_import_running(self, running):
        """Toggle cancellation control for one active import."""
        self.import_cancel_button.setVisible(bool(running))
        if not running:
            self.import_progress_label.setText("")

    @staticmethod
    def _table(headers):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        table.verticalHeader().hide()
        table.verticalHeader().setDefaultSectionSize(44)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.setShowGrid(False)
        return table

    @staticmethod
    def _table_area(table, empty):
        """Keep an empty-state message in the table slot without hiding the add form."""
        area = QStackedWidget()
        area.addWidget(table)
        area.addWidget(empty)
        return area

    @staticmethod
    def _metric(title, value):
        frame = QFrame()
        frame.setObjectName("metric")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 9, 12, 9)
        layout.setSpacing(3)
        layout.addWidget(_plain_label(title, "muted"))
        value_label = _plain_label(value, "metricValue")
        layout.addWidget(value_label)
        return frame, value_label

    def set_snapshot(self, data):
        """Refresh all tables from a service snapshot without emitting edit commands."""
        active_tab = self.tabs.currentIndex()
        self._capture_source_edits()
        self._updating = True
        self.running = bool(data.get("running"))
        parsing = str(data.get("parsing") or "неизвестно")
        telegram = str(data.get("telegram") or "неизвестно")
        self.runtime_status.setText("Парсинг: " + parsing)
        self._set_state(self.runtime_status, "ok" if parsing == "работает" else "warning" if parsing == "пауза" else "off")
        self.connection_card[1].setText(telegram.capitalize())
        self.toggle_button.setText("Поставить на паузу" if self.running else "Запустить парсинг")
        self.autostart.setChecked(bool(data.get("parsing_autostart", True)))
        self._fill_marketplaces(list(data.get("marketplaces") or []))
        user = data.get("telegram_user") or {}
        user_status = str(user.get("status") or "не настроен")
        self.user_status.setText("User API: " + user_status)
        self._set_state(self.user_status, "ok" if user_status == "подключён" else "warning" if user_status == "требует входа" else "off")
        self._fill_telegram(list(data.get("sources") or []), telegram, data.get("source_states") or {}, user)
        self.tabs.setCurrentIndex(active_tab)
        self._updating = False

    def _fill_marketplaces(self, entries):
        self.marketplaces_area.setCurrentWidget(self.marketplaces_table if entries else self.marketplaces_empty)
        self.marketplaces_table.setRowCount(len(entries))
        enabled_count = 0
        for row, entry in enumerate(entries):
            enabled = bool(entry.get("enabled"))
            enabled_count += enabled
            toggle = SourceToggleButton(enabled)
            toggle.toggled.connect(partial(self._marketplace_toggled, str(entry['id'])))
            self.marketplaces_table.setCellWidget(row, 0, self._source_control(toggle))
            self.marketplaces_table.setCellWidget(
                row, 1, self._marketplace_name_cell(
                    str(entry.get("name") or "Без названия"), bool(entry.get("builtin")), str(entry["id"]),
                ),
            )
            self._item(self.marketplaces_table, row, 2, str(entry.get("url") or ""))
            status = _plain_label(str(entry.get("status") or "неизвестно"), "tableStatus")
            self._set_state(status, self._status_kind(entry.get("status")))
            self.marketplaces_table.setCellWidget(row, 3, status)
            self._item(self.marketplaces_table, row, 4, str(entry.get("detail") or ""))
        self.marketplaces_card[1].setText(f"{enabled_count} из {len(entries)} включено")

    def _marketplace_name_cell(self, name, builtin, marketplace_id):
        """Place source-specific actions beside the name, where they stay readable."""
        cell = QWidget()
        layout = QHBoxLayout(cell)
        layout.setContentsMargins(7, 0, 7, 0)
        layout.setSpacing(8)
        label = _plain_label(name)
        label.setToolTip(name)
        layout.addWidget(label, 1)
        if builtin:
            badge = _plain_label("Встроенная", "builtInBadge")
            badge.setToolTip("Встроенную биржу можно включить или выключить, но нельзя удалить.")
            layout.addWidget(badge)
        else:
            remove = QPushButton("Удалить")
            remove.setObjectName("danger")
            remove.setAccessibleName(f"Удалить сайт {name}")
            remove.clicked.connect(partial(self._remove_marketplace, marketplace_id))
            layout.addWidget(remove)
        return cell

    def _fill_telegram(self, sources, telegram_status, source_states, user_api=None):
        self.telegram_area.setCurrentWidget(self.telegram_table if sources else self.telegram_empty)
        self.telegram_table.setRowCount(len(sources))
        active = 0
        for row, source in enumerate(sources):
            active += bool(source.enabled)
            stable_id = str(source.source_id or source.chat_id)
            saved_edit = self._source_edits.get(stable_id, {})
            enabled = SourceToggleButton(source.enabled)
            self.telegram_table.setCellWidget(row, 0, self._source_control(enabled))
            name = QLineEdit(saved_edit.get("name", source.name))
            name.setPlaceholderText("Название")
            self.telegram_table.setCellWidget(row, 1, name)
            self._item(self.telegram_table, row, 2, source.chat_id)
            self.telegram_table.item(row, 2).setData(Qt.ItemDataRole.UserRole, stable_id)
            language = QComboBox()
            language.addItem("Русский", "ru")
            language.addItem("English", "en")
            language.setCurrentIndex(max(0, language.findData(saved_edit.get("language", source.language))))
            self.telegram_table.setCellWidget(row, 3, language)
            interval = QSpinBox()
            interval.setRange(30, 86400)
            interval.setValue(saved_edit.get("interval", source.poll_interval_seconds))
            interval.setSuffix(" сек")
            self.telegram_table.setCellWidget(row, 4, interval)
            source_state = source_states.get(source.chat_id, {})
            if source.access_mode == "user":
                if source.access_status == "available" and bool((user_api or {}).get("connected")):
                    status_text = "User API: работает"
                elif source.access_status == "available":
                    status_text = "User API: ожидание связи"
                else:
                    status_text = "User API: " + source.access_status
            elif source.pending_connection:
                status_text = "ожидает подключения"
            elif not source.enabled:
                status_text = "выключен"
            elif source_state.get("last_error_code"):
                status_text = "ошибка обработки"
            elif self.running and telegram_status == "подключён":
                status_text = "работает"
            elif self.running:
                status_text = "ожидание связи"
            else:
                status_text = "пауза"
            status = _plain_label(status_text, "tableStatus")
            self._set_state(status, self._status_kind(status_text))
            self.telegram_table.setCellWidget(row, 5, status)
            actions = QWidget()
            action_layout = QHBoxLayout(actions)
            action_layout.setContentsMargins(0, 0, 0, 0)
            action_layout.setSpacing(5)
            save = QPushButton("Сохранить")
            save.setAccessibleName(f"Сохранить изменения Telegram-источника {source.chat_id}")
            save.clicked.connect(partial(self._save_source_row, stable_id, enabled, name, language, interval))
            remove = QPushButton("Удалить")
            remove.setObjectName("danger")
            remove.setAccessibleName(f"Удалить Telegram-источник {source.chat_id}")
            remove.clicked.connect(partial(self._remove_source, stable_id, source.name))
            action_layout.addWidget(save)
            action_layout.addWidget(remove)
            self.telegram_table.setCellWidget(row, 6, actions)
            enabled.toggled.connect(partial(self._source_toggled, stable_id))
            name.textChanged.connect(partial(self._remember_source_edit, stable_id, name, language, interval))
            language.currentIndexChanged.connect(partial(self._remember_source_edit, stable_id, name, language, interval))
            interval.valueChanged.connect(partial(self._remember_source_edit, stable_id, name, language, interval))
        self.sources_card[1].setText(f"{active} из {len(sources)} активно")

    def _capture_source_edits(self):
        for row in range(self.telegram_table.rowCount()):
            source_cell = self.telegram_table.item(row, 2)
            if source_cell is None:
                continue
            source_id = str(source_cell.data(Qt.ItemDataRole.UserRole) or source_cell.text())
            name = self.telegram_table.cellWidget(row, 1)
            language = self.telegram_table.cellWidget(row, 3)
            interval = self.telegram_table.cellWidget(row, 4)
            if name and language and interval:
                self._remember_source_edit(source_id, name, language, interval)

    def _remember_source_edit(self, source_id, name, language, interval, *_args):
        self._source_edits[source_id] = {
            "name": name.text(), "language": str(language.currentData()), "interval": interval.value(),
        }

    @staticmethod
    def _source_control(toggle):
        """Pair the coloured lamp with text so its meaning is never colour-only."""
        holder = QWidget()
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(6, 0, 4, 0)
        layout.setSpacing(5)
        state = _plain_label("", "sourceState")

        def update_state(enabled):
            state.setText("Вкл." if enabled else "Выкл.")
            state.setProperty("enabledState", "on" if enabled else "off")
            state.style().unpolish(state)
            state.style().polish(state)

        update_state(toggle.isChecked())
        toggle.toggled.connect(update_state)
        layout.addWidget(toggle)
        layout.addWidget(state)
        return holder

    @staticmethod
    def _item(table, row, column, text):
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        item.setToolTip(text)
        table.setItem(row, column, item)

    @staticmethod
    def _set_state(widget, state):
        widget.setProperty("state", state)
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    @staticmethod
    def _status_kind(status):
        status = str(status or "").casefold()
        if status == "работает" or status.endswith(": работает"):
            return "ok"
        if status in {"пауза", "ожидание связи", "требует проверки"} or status.endswith(": ожидание связи") or "flood_wait" in status:
            return "warning"
        return "off"

    def show_feedback(self, text, error=False):
        """Show a concise operation result without exposing internal exception details."""
        self.feedback.setText(text)
        self.feedback.setProperty("error", bool(error))
        self.feedback.style().unpolish(self.feedback)
        self.feedback.style().polish(self.feedback)

    def _request_toggle(self):
        self.toggle_requested.emit(not self.running)
        self.show_feedback("Команда отправлена…")

    def _autostart_changed(self, enabled):
        if self._updating:
            return
        if enabled:
            answer = QMessageBox.question(
                self, "Включить автозапуск парсинга?",
                "При следующем запуске программы сервисы парсинга запустятся автоматически. Продолжить?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                self._updating = True
                self.autostart.setChecked(False)
                self._updating = False
                return
        self.autostart_requested.emit(enabled)
        self.show_feedback("Настройка автозапуска сохраняется…")

    def _add_marketplace(self):
        name, url = self.marketplace_name.text().strip(), self.marketplace_url.text().strip()
        if not name or not url:
            self.show_feedback("Укажите название и полный адрес сайта.", True)
            return
        self.add_marketplace_requested.emit(name, url)
        self.marketplace_name.clear()
        self.marketplace_url.clear()
        self.show_feedback("Сайт добавляется…")

    def _marketplace_toggled(self, marketplace_id, enabled):
        if not self._updating:
            self.marketplace_toggle_requested.emit(marketplace_id, enabled)
            self.show_feedback("Состояние источника сохраняется…")

    def _source_toggled(self, source_id, enabled):
        if not self._updating:
            self.source_toggle_requested.emit(source_id, enabled)
            self.show_feedback("Состояние Telegram-источника сохраняется…")

    def _remove_marketplace(self, marketplace_id):
        answer = QMessageBox.question(self, "Удалить сайт?", "Пользовательский сайт будет удалён из таблицы парсинга.")
        if answer == QMessageBox.StandardButton.Yes:
            self.remove_marketplace_requested.emit(marketplace_id)

    def _add_source(self):
        source_id = self.new_source_id.text().strip()
        if not source_id:
            self.show_feedback("Введите @username, ссылку t.me или числовой ID Telegram-чата.", True)
            return
        self.add_source_requested.emit(
            source_id, self.new_source_name.text().strip(), True,
            str(self.new_source_language.currentData()), self.new_source_interval.value(),
        )
        self.new_source_id.clear()
        self.new_source_name.clear()
        self.show_feedback("Telegram-источник добавляется…")

    def _begin_user_login(self):
        api_id, api_hash, phone = self.user_api_id.text().strip(), self.user_api_hash.text().strip(), self.user_phone.text().strip()
        if not api_id or not api_hash or not phone:
            self.show_feedback("Укажите API ID, API Hash и номер телефона.", True)
            return
        self.user_login_requested.emit(api_id, api_hash, phone)
        self.user_api_hash.clear()
        self.show_feedback("Код подтверждения запрашивается…")

    def set_user_challenge(self, challenge):
        """Reveal the next authorisation step without retaining credentials in a label."""
        self._user_challenge = str(challenge or "")
        self._user_code_holder.setVisible(bool(self._user_challenge))
        self._user_password_holder.hide()

    def _submit_user_code(self):
        if not self._user_challenge or not self.user_code.text().strip():
            self.show_feedback("Введите код из Telegram.", True)
            return
        self.user_code_requested.emit(self._user_challenge, self.user_code.text().strip())
        self.user_code.clear()

    def set_user_password_required(self):
        self._user_code_holder.hide()
        self._user_password_holder.show()
        self.show_feedback("Telegram запросил пароль двухфакторной авторизации.")

    def _submit_user_password(self):
        if not self._user_challenge or not self.user_password.text():
            self.show_feedback("Введите пароль 2FA.", True)
            return
        self.user_password_requested.emit(self._user_challenge, self.user_password.text())
        self.user_password.clear()

    def complete_user_login(self):
        self._user_challenge = ""
        self._user_code_holder.hide()
        self._user_password_holder.hide()
        self.user_phone.clear()
        self.show_feedback("Telegram User API подключён.")

    def _forget_user(self):
        answer = QMessageBox.question(self, "Удалить подключение?", "Сессия будет отозвана в Telegram и удалена с этого компьютера.")
        if answer == QMessageBox.StandardButton.Yes:
            self.user_disconnect_requested.emit(True)

    def _save_source_row(self, source_id, enabled, name, language, interval):
        self._source_edits.pop(source_id, None)
        self.update_source_requested.emit(
            source_id, name.text().strip(), enabled.isChecked(),
            str(language.currentData()), interval.value(),
        )
        self.show_feedback("Параметры Telegram-источника сохраняются…")

    def _remove_source(self, source_id, source_name):
        display_name = source_name.strip() or "Без названия"
        answer = QMessageBox.question(
            self, "Удалить Telegram-источник?",
            f"«{display_name}»\nID: {source_id}\n\nИсточник будет удалён из чтения. Уже найденные заказы останутся в базе.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._source_edits.pop(source_id, None)
            self.remove_source_requested.emit(source_id)
