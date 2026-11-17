"""Shared colors and Qt stylesheet matching the approved desktop reference."""

STYLE = '''
QWidget { background: #0c1825; color: #e7efff; font-family: 'Segoe UI'; font-size: 13px; }
QMainWindow { background: #0c1825; }
QLabel { background: transparent; }
QLabel#title { font-size: 24px; font-weight: 700; }
QLabel#detailTitle { font-size: 20px; font-weight: 600; }
QFrame#expanded { background: #103f79; border: none; }
QFrame#confirmation { background: #29291f; border: 1px solid #8e742b; border-radius: 7px; }
QStatusBar { color: #91a8c4; font-size: 11px; }
QLabel#section { font-size: 16px; font-weight: 600; color: #a9d6ff; }
QLabel#muted { color: #91a8c4; }
QLabel#notice { color: #f4c465; padding: 8px; }
QFrame#sidebar { background: #08131f; border-right: 1px solid #213347; }
QFrame#journalDock { background: transparent; border: none; }
QFrame#panel { background: #0e1c2a; border: 1px solid #253b50; border-radius: 8px; }
QFrame#metric { background: #172738; border: 1px solid #283d53; border-radius: 6px; }
QLabel#feedTag { background: #1a477d; color: #d9ecff; border: 1px solid #2d629e; border-radius: 11px; padding: 3px 9px; font-size: 11px; }
QLabel#chip { background: #112333; color: #a6c8e8; border: 1px solid #294155; border-radius: 13px; padding: 6px 10px; font-size: 11px; }
QLabel#chip[state="ok"] { color: #43df8c; border-color: #174d3d; background: #0d2927; }
QLabel#chip[state="warning"] { color: #ffc55a; border-color: #544622; background: #29271d; }
QLabel#chip[state="off"] { color: #91a8c4; border-color: #294155; background: #112333; }
QPushButton#statusChip { background: #112333; color: #a6c8e8; border: 1px solid #294155; border-radius: 13px; padding: 6px 10px; font-size: 11px; }
QPushButton#statusChip:hover { background: #19344a; border-color: #477496; color: #ffffff; }
QPushButton#statusChip[state="ok"] { color: #43df8c; border-color: #174d3d; background: #0d2927; }
QPushButton#statusChip[state="warning"] { color: #ffc55a; border-color: #544622; background: #29271d; }
QPushButton#statusChip[state="off"] { color: #91a8c4; border-color: #294155; background: #112333; }
QPushButton { background: #152638; border: 1px solid #30475f; border-radius: 6px; padding: 8px 12px; }
QPushButton:hover { background: #203c57; border-color: #5082ac; }
QPushButton:pressed { background: #173c6a; }
QPushButton:focus { border: 1px solid #68caff; }
QPushButton:disabled { color: #65798c; background: #14202d; border-color: #263747; }
QPushButton#primary { background: #0879f9; color: white; border: 1px solid #299aff; font-weight: 600; }
QPushButton#primary:hover { background: #168cff; }
QPushButton#primary:disabled { background: #263544; color: #79899a; border-color: #344452; }
QPushButton#danger { color: #ff9aa4; border-color: #71404a; background: #2d2028; }
QPushButton#danger:hover { color: #ffffff; border-color: #b45c68; background: #482832; }
QPushButton#nav { text-align: left; padding: 12px; border: none; background: transparent; font-size: 15px; }
QPushButton#nav:checked { background: #06489f; border: 1px solid #0767d5; }
QPushButton#journalTab { background: #102c45; border: 1px solid #467497; border-bottom: none; border-top-left-radius: 10px; border-top-right-radius: 10px; border-bottom-left-radius: 0; border-bottom-right-radius: 0; padding: 7px 15px; color: #d9ecff; font-size: 13px; font-weight: 600; }
QPushButton#journalTab:hover { background: #164565; border-color: #69a4d0; color: #ffffff; }
QPushButton#journalTab:pressed { background: #0c2439; }
QLineEdit, QPlainTextEdit, QComboBox, QSpinBox { background: #142334; border: 1px solid #30465d; border-radius: 6px; padding: 8px; selection-background-color: #155caf; }
QLineEdit:focus, QPlainTextEdit:focus { border-color: #278bed; }
QComboBox QAbstractItemView { background: #142334; selection-background-color: #1453a0; }
QListWidget { background: #0e1c2a; border: 1px solid #253b50; padding: 6px; }
QListWidget::item { padding: 12px; border-bottom: 1px solid #213347; }
QListWidget::item:selected { background: #123e74; }
QTableView { background: #0c1a28; alternate-background-color: #0f1e2e; border: 1px solid #283c50; border-radius: 6px; gridline-color: #233649; selection-background-color: #103f79; outline: none; }
QHeaderView::section { background: #112234; color: #b2c7e2; border: none; border-bottom: 1px solid #2b435d; padding: 10px 6px; font-weight: 400; }
QTabBar::tab { background: #102030; color: #aec4df; padding: 11px 16px; border-bottom: 3px solid transparent; }
QTabBar::tab:selected { background: #142e4c; color: white; border-bottom: 3px solid #41c7ff; }
QTabWidget::pane { border: none; }
QScrollArea { border: none; }
QScrollBar:vertical { background: #0d1b29; width: 9px; margin: 0; }
QScrollBar::handle:vertical { background: #34516f; min-height: 30px; border-radius: 4px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: #0d1b29; height: 9px; }
QScrollBar::handle:horizontal { background: #34516f; min-width: 30px; }
QSplitter::handle { background: #15283b; }
QToolTip { background: #20344a; color: white; border: 1px solid #41617e; padding: 6px; }
QCheckBox { spacing: 8px; background: transparent; }
QDialog QLabel#dialogTitle { font-size: 22px; font-weight: 700; }
QDialog QLabel#dialogStatus { border: 1px solid #294155; border-radius: 13px; padding: 6px 12px; color: #a6c8e8; }
QDialog QLabel#dialogStatus[state="ok"] { color: #43df8c; border-color: #174d3d; background: #0d2927; }
QDialog QLabel#dialogStatus[state="warning"] { color: #ffc55a; border-color: #544622; background: #29271d; }
QDialog QLabel#dialogStatus[state="off"] { color: #91a8c4; border-color: #294155; background: #112333; }
QDialog QLabel#metricValue { font-size: 16px; font-weight: 600; color: #edf6ff; }
QDialog QLabel#dialogNotice { color: #9db4cc; background: #102131; border: 1px solid #294155; border-radius: 7px; padding: 10px; }
QDialog QLabel#muted[error="true"] { color: #ff7e8a; }
QDialog QLabel#emptyState { color: #a9c6e3; background: #102131; border: 1px dashed #3a5b78; border-radius: 7px; padding: 18px; }
QDialog QLabel#builtInBadge { color: #a9c6e3; background: #172a3d; border: 1px solid #38536d; border-radius: 9px; padding: 3px 7px; font-size: 11px; }
QDialog QLabel#tableStatus { border: 1px solid #294155; border-radius: 10px; padding: 4px 8px; color: #91a8c4; }
QDialog QLabel#tableStatus[state="ok"] { color: #43df8c; border-color: #174d3d; background: #0d2927; }
QDialog QLabel#tableStatus[state="warning"] { color: #ffc55a; border-color: #544622; background: #29271d; }
QDialog QLabel#tableStatus[state="off"] { color: #91a8c4; border-color: #294155; background: #112333; }
QDialog QTableWidget { background: #0c1a28; alternate-background-color: #102131; border: 1px solid #283c50; border-radius: 7px; }
QDialog QTableWidget::item { padding: 7px; border-bottom: 1px solid #203449; }
QPushButton#sourceToggle { min-width: 22px; max-width: 22px; min-height: 22px; max-height: 22px; padding: 0; border-radius: 11px; }
QPushButton#sourceToggle[enabledState="on"] { background: #2ac776; border: 2px solid #91ffd0; }
QPushButton#sourceToggle[enabledState="on"]:hover { background: #53e394; border-color: #d0ffe8; }
QPushButton#sourceToggle[enabledState="off"] { background: #c74456; border: 2px solid #ffabb5; }
QPushButton#sourceToggle[enabledState="off"]:hover { background: #e85b6d; border-color: #ffd3d8; }
QPushButton#sourceToggle:focus { border: 3px solid #8fdbff; }
QLabel#sourceState { font-size: 11px; font-weight: 600; }
QLabel#sourceState[enabledState="on"] { color: #7bf1b2; }
QLabel#sourceState[enabledState="off"] { color: #ff9da8; }
'''
