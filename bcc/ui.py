"""Sole user interface: Modern Qt Dashboard."""

import csv
import math
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import VERSION
from .chart import CandleChart
from .config import STRATEGIES, STRATEGY_NAMES, defaults
from .lifecycle import Lifecycle
from .runtime_health import observe as observe_runtime
from .supervisor import Supervisor
from .market_clock import quote_is_recent
from .position_report import position_mark_pnl
from .preferences import DEFAULT_PREFERENCES, normalize_preferences
from .storage import Repository, Store, atomic_json, load_json
from .flow_layout import FlowLayout
from .trading import (
    available_margin,
    equity,
    position_unrealized,
    position_initial_margin,
)
from .widgets import Fields, button, combo, confirm_dialog, label
from .wizard import BotWizard

STYLE = """
QWidget {color:#e5edf8;font-family:"Segoe UI","DejaVu Sans";font-size:13px;}
QMainWindow,QDialog,QWidget#root,QWidget[scrollContent="true"] {background:#101722;}
QFrame#topbar,QWidget#aside {background:#142130;border:1px solid #2a3b50;border-radius:8px;}
QLabel#headline {font-size:23px;font-weight:700;color:#f1f7ff;}
QLabel#title {font-size:17px;font-weight:600;color:#e6f4ff;}
QLabel#muted {color:#9eb1c9;}
QLabel#error {color:#f4b178;}
QLabel#pill {color:#39deca;border:1px solid #287f7a;padding:5px 14px;border-radius:6px;}
QPushButton {background:#1b2b40;border:1px solid #32465e;padding:9px 15px;border-radius:6px;}
QPushButton:hover {background:#263e55;border-color:#40baa9;}
QPushButton#primary {background:#079e96;color:#fff;font-weight:700;border-color:#25c9b9;}
QPushButton:disabled {color:#657489;background:#17212d;}
QLineEdit,QComboBox,QSpinBox,QDoubleSpinBox {background:#172637;border:1px solid #35475c;border-radius:5px;padding:7px;min-height:18px;}
QLineEdit:focus,QComboBox:focus,QDoubleSpinBox:focus {border-color:#2bd4c0;}
QComboBox QAbstractItemView {background:#1b2b40;selection-background-color:#215d66;}
QListWidget {background:#142130;color:#e5edf8;border:1px solid #293b50;selection-background-color:#1a535c;selection-color:#ffffff;}
QListWidget::item {padding:6px;}
QListWidget::item:hover {background:#1b2d40;}
QListWidget::item:selected,QListWidget::item:selected:!active {background:#1a535c;color:#ffffff;}
QTableWidget {background:#142130;alternate-background-color:#172738;gridline-color:#24364b;border:1px solid #293b50;selection-background-color:#1a535c;}
QHeaderView::section {background:#1b2d40;color:#a9c1d9;border:0;padding:10px;font-weight:600;}
QHeaderView,QTableCornerButton::section {background:#1b2d40;border:0;}
QTableWidget::item {padding:7px;}
QTabWidget::pane {border:1px solid #2b4057;border-radius:6px;}
QTabBar::tab {background:#162435;padding:11px 16px;color:#a7bdd3;}
QTabBar::tab:selected {background:#1b3c49;color:#46dfcc;border-bottom:2px solid #23cfba;}
QListWidget#steps {background:#101d2c;border:0;outline:0;}
QListWidget#steps::item {padding:17px 12px;border-radius:5px;color:#aebfd4;}
QListWidget#steps::item:selected {background:#18424c;color:#45dfce;}
QFrame#condition {background:#17283a;border:1px solid #304257;border-radius:7px;}
QScrollArea {background:transparent;border:0;}
QScrollBar:vertical {background:#122030;width:11px;}
QScrollBar::handle:vertical {background:#36526a;min-height:28px;border-radius:4px;}
QPlainTextEdit {background:#111e2d;border:1px solid #2d4258;padding:8px;}
QMenu {background:#1b2d40;border:1px solid #40566c;}
QMenu::item {padding:9px 24px;} QMenu::item:selected {background:#245560;}
QCheckBox {spacing:9px;padding:5px;}
QWidget#strategyTile {background:#142130;border:1px solid #2e435a;border-radius:9px;}
QWidget#strategyTile:hover {border-color:#36b8ab;}
QPushButton#strategyChoose {background:#12545c;border-color:#259e9c;color:#e8ffff;}
QTreeWidget {background:#142130;alternate-background-color:#172738;color:#e5edf8;border:1px solid #293b50;selection-background-color:#1a535c;selection-color:#ffffff;outline:0;}
QTreeWidget::item {color:#e5edf8;padding:5px 7px;min-height:20px;}
QTreeWidget::item:hover {background:#263e55;}
QTreeWidget::item:selected,QTreeWidget::item:selected:!active {background:#1a535c;color:#ffffff;}
QTreeWidget#botsTree {background:#122031;border:1px solid #293e53;}
QTreeWidget#botsTree::item {padding:9px 5px;min-height:24px;}
QTreeWidget#botsTree::item:selected {background:#1c535e;color:#4bdeca;}
/* Approved Modern Qt wizard: actual constructor, not a visual preview. */
QDialog#botWizard {background:#091929;}
QFrame#wizardBanner {background:#0c2134;border:1px solid #234357;border-radius:11px;}
QLabel#wizardHeading {font-size:20px;font-weight:700;color:#effaff;}
QFrame#wizardSidebar {background:#0e2235;border:1px solid #214159;border-radius:10px;}
QLabel#sidebarTitle {color:#f0f9ff;font-weight:700;font-size:16px;padding:4px 8px;}
QListWidget#steps {background:transparent;border:0;outline:0;}
QListWidget#steps::item {background:transparent;color:#aebed0;border-radius:9px;
    border:1px solid transparent;padding:12px 9px;margin-bottom:5px;}
QListWidget#steps::item:hover {background:#153448;color:#f1fcff;}
QListWidget#steps::item:selected,QListWidget#steps::item:selected:!active {
    background:#104853;color:#4ef1dc;border-left:3px solid #13d3c0;
    font-weight:600;border-radius:9px;
}
QLabel#stageHeading {color:#f5fbff;font-size:19px;font-weight:700;}
QFrame#wizardSummary {background:#0d2134;border:1px solid #234258;border-radius:10px;}
QLabel#wizardSummaryValues {background:#0d2134;padding:5px 0;color:#e5edf8;}
QScrollArea#wizardSummaryScroll {background:#0d2134;border:0;}
QWidget#polyTabPanel {background:#0c1f31;}
QFrame#wizardSection {background:#132b40;border:1px solid #2b5065;border-radius:9px;}
QTabWidget#polyConditionTabs::pane {background:#0c1f31;border:1px solid #254457;
    border-radius:9px;top:-1px;}
QTabWidget#polyConditionTabs QTabBar::tab {background:#10293d;color:#b9cadb;
    border:1px solid #294a5e;padding:13px 12px;margin-right:2px;}
QTabWidget#polyConditionTabs QTabBar::tab:selected {background:#0c3e48;color:#48f2da;
    border-bottom:3px solid #19d3bd;font-weight:700;}
QTabWidget#polyConditionTabs QTabBar::tab:hover {background:#1a4254;}
QPushButton#strategyNav {background:#14283b;border:1px solid #2c465c;border-radius:7px;
    color:#c6d5e7;padding:10px 14px;}
QPushButton#strategyNav:checked {background:#163d4b;color:#4ff1e0;
    border-bottom:3px solid #21d5c2;font-weight:700;}
QPushButton#strategyNav:hover {background:#23485b;}
QPushButton#wizardSave {background:#0ed1bf;color:#062130;font-weight:700;
    border:1px solid #41ebd7;padding:11px 20px;}
QPushButton#wizardSave:hover {background:#45f0d9;}
QPushButton#wizardDraft {background:#142e45;border:1px solid #315b70;}
QPushButton#wizardCancel {background:transparent;border-color:#2d4a62;}
QLabel#timingExample {color:#64dcce;padding:8px;}
QTableWidget#polyEffectiveMarketMatrix {background:#10293e;alternate-background-color:#153247;}
QLabel#wizardStepCounter {color:#9cefe4;font-weight:700;padding:8px 14px;
    background:#103b45;border:1px solid #235f68;border-radius:7px;}
QFrame#dashboardActionArea {background:#0d2031;border:1px solid #254158;border-radius:9px;}
QLabel#dashboardMetrics {background:#0e2434;border:1px solid #24516a;
    border-radius:8px;padding:11px 17px;color:#e3f4ff;}
QLabel#wizardStageProgress {color:#58daca;padding:0 8px 4px 8px;}
QLabel#wizardPaperMode {color:#39e0cc;background:#0d3b40;border:1px solid #188f88;
    border-radius:6px;padding:8px 12px;}
QLabel#wizardSummaryHeading {color:#eafaff;font-weight:700;}
QFrame#wizardSection QFormLayout {background:transparent;}
QTableWidget#polyEffectiveMarketMatrix::item:selected {background:#0f6570;color:#ffffff;}
QDialog#botWizard QLineEdit,QDialog#botWizard QComboBox,
QDialog#botWizard QSpinBox,QDialog#botWizard QDoubleSpinBox {min-height:24px;padding:9px;}
QLabel#stageHeading {padding:1px 0 3px 0;}
QFrame#polyRulePreview {background:#0d2638;border-color:#257f83;}

QFrame#polyRulePreview {background:#0f273a;border:1px solid #296078;border-radius:10px;}
QLabel#polyRulePreviewValues {color:#e5f6ff;background:transparent;padding:2px;}
QFrame#polyRulePreview QPushButton {padding:10px 8px;background:#143a4d;border-color:#2a6574;}
QFrame#polyRulePreview QPushButton:hover {background:#1d5261;border-color:#35c6b6;}
"""


class SortItem(QTableWidgetItem):
    def __lt__(self, other):
        def key(text):
            try:
                value = float(text.replace(" ", ""))
                if math.isfinite(value):
                    return (0, value)
            except ValueError:
                pass
            return (1, text.casefold())

        return key(self.text()) < key(other.text())


def table(headers):
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.setAlternatingRowColors(True)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    t.verticalHeader().hide()
    t.verticalHeader().setDefaultSectionSize(38)
    t.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    t.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
    t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    t.setSortingEnabled(True)
    t.sortByColumn(0, Qt.SortOrder.AscendingOrder)
    return t


def populate(t, rows, keys=None):
    item = t.item(t.currentRow(), 0)
    selected_key = item.data(Qt.ItemDataRole.UserRole) if item else None
    sorting = t.isSortingEnabled()
    t.setSortingEnabled(False)
    t.clearSelection()
    t.setCurrentCell(-1, -1)
    t.clearContents()
    t.setRowCount(len(rows))
    for i, row in enumerate(rows):
        for j, value in enumerate(row):
            item = SortItem(str(value))
            if keys is not None:
                item.setData(Qt.ItemDataRole.UserRole, keys[i])
            t.setItem(i, j, item)
    t.setSortingEnabled(sorting)
    if selected_key is not None:
        for i in range(t.rowCount()):
            if t.item(i, 0).data(Qt.ItemDataRole.UserRole) == selected_key:
                t.selectRow(i)
                break


class MainWindow(QMainWindow):
    def __init__(self, root):
        super().__init__()
        self.repo = Repository(root)
        self.life = Lifecycle(self.repo)
        self.supervisor = Supervisor(self.repo, lifecycle=self.life)
        self.current = None
        self.rows = []
        self.monitor_state = None
        self.running_monitor = None
        self.setWindowTitle("Bot Control Center Pro " + VERSION)
        screen = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1500, screen.width() - 40), min(960, screen.height() - 60))
        self.preferences_path = self.repo.root / "preferences.json"
        try:
            self.preferences = normalize_preferences(load_json(self.preferences_path))
        except (OSError, ValueError, TypeError):
            self.preferences = DEFAULT_PREFERENCES.copy()
        central = QWidget()
        central.setObjectName("root")
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(20, 14, 20, 14)
        layout.setSpacing(14)
        top = QFrame()
        top.setObjectName("topbar")
        bar = QHBoxLayout(top)
        brand = label("▣  BOT CONTROL CENTER PRO", "title")
        brand.setWordWrap(False)
        bar.addWidget(brand)
        navigation = QToolBar()
        navigation.setMovable(False)
        navigation.setMinimumWidth(0)
        navigation.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        bar.addWidget(navigation, 1)
        self.strategy_buttons = {}
        for key, title in [
            ("VELES", "VELES"),
            ("POLYMARKET", "Polymarket"),
            ("PUMP_DUMP", "PUMP&DUMP"),
            ("GRID", "GRID"),
        ]:
            control = button(title, lambda _checked=False, s=key: self.select_strategy(s))
            control.setObjectName("strategyNav")
            control.setCheckable(True)
            self.strategy_buttons[key] = control
            navigation.addWidget(control)
        self.other = combo(
            [("Другие стратегии", None)]
            + [(STRATEGY_NAMES[s], s) for s in STRATEGIES[4:]]
        )
        self.other.activated.connect(
            lambda _: (
                self.select_strategy(self.other.currentData())
                if self.other.currentData()
                else None
            )
        )
        navigation.addWidget(self.other)
        self.runtime_status = label("Служба: проверка", "muted")
        self.runtime_status.setObjectName("runtimeServiceState")
        bar.addWidget(self.runtime_status)
        bar.addWidget(button("⚙ Интерфейс", self.app_settings))
        layout.addWidget(top)
        header = QHBoxLayout()
        self.title = label("Все боты", "headline")
        header.addWidget(self.title)
        header.addStretch()
        self.filter = combo(
            [("Все стратегии", None)] + [(STRATEGY_NAMES[s], s) for s in STRATEGIES]
        )
        self.filter.currentIndexChanged.connect(self.refresh)
        self.filter.currentIndexChanged.connect(self.update_strategy_navigation)
        header.addWidget(self.filter)
        self.search = QLineEdit()
        self.search.setPlaceholderText("⌕  Поиск бота или монеты")
        self.search.textChanged.connect(self.refresh)
        header.addWidget(self.search)
        header.addWidget(button("+ Новый бот", self.create_bot, "primary"))
        layout.addLayout(header)
        # The approved dashboard separates running controls from management.
        # These are the SAME lifecycle actions as in R4, not preview buttons.
        action_area = QFrame()
        action_area.setObjectName("dashboardActionArea")
        action_layout = QVBoxLayout(action_area)
        action_layout.setContentsMargins(10, 8, 10, 8)
        action_layout.setSpacing(6)
        lifecycle_row = FlowLayout()
        lifecycle_row.setSpacing(6)
        lifecycle_row.addWidget(label("Управление", "muted"))
        monitor_button = button("Монитор всех ботов", self.show_running_bots, "primary")
        monitor_button.setObjectName("openRunningBots")
        lifecycle_row.addWidget(monitor_button)
        report_button = button("Сохранить отчёт", self.save_current_report)
        report_button.setObjectName("saveCurrentBotsReport")
        lifecycle_row.addWidget(report_button)
        risk_button = button("Общий риск", self.shared_risk_settings)
        risk_button.setObjectName("sharedRiskSettings")
        lifecycle_row.addWidget(risk_button)
        for title, fn in [
            ("▶ Запуск", self.start),
            ("■ Стоп", self.stop),
            ("После сделок", self.finish),
            ("Принудительно", self.force),
        ]:
            lifecycle_row.addWidget(button(title, fn))
        action_layout.addLayout(lifecycle_row)
        manage_row = FlowLayout()
        manage_row.setSpacing(6)
        manage_row.addWidget(label("Параметры и файлы", "muted"))
        for title, fn in [
            ("Редактировать", self.edit),
            ("Просмотр параметров", self.view_settings),
            ("Создать копию", self.copy_bot),
            ("Удалить", self.delete),
            ("Импорт / экспорт", self.transfer_menu),
        ]:
            manage_row.addWidget(button(title, fn))
        action_layout.addLayout(manage_row)
        layout.addWidget(action_area)
        self.metrics = label("", "title")
        self.metrics.setObjectName("dashboardMetrics")
        layout.addWidget(self.metrics)
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setObjectName("botDetailsSplitter")
        splitter.setChildrenCollapsible(False)
        self.bots_table = table(
            [
                "Бот",
                "Стратегия",
                "Монеты",
                "Процесс",
                "Баланс PAPER",
                "Позиции",
                "Результат",
            ]
        )
        self.bots_table.itemSelectionChanged.connect(self.selection)
        self.bots_table.doubleClicked.connect(self.view_settings)
        self.bots_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.bots_table.customContextMenuRequested.connect(self.context_menu)
        self.bots_table.setMinimumHeight(100)
        splitter.addWidget(self.bots_table)
        self.tabs = QTabWidget()
        self.positions = table(
            [
                "Монета",
                "Секция",
                "Сторона",
                "Количество",
                "Средняя цена",
                "Маржа",
                "Нереализ. PnL",
                "DCA сейчас",
                "Открыта",
            ]
        )
        self.orders = table(
            ["Монета", "Секция", "Ордер", "Цена", "Доля, %", "Состояние"]
        )
        self.history = table(
            [
                "Время UTC",
                "Монета",
                "Секция",
                "Действие",
                "Цена",
                "Количество",
                "Net PnL",
                "Причина",
                "Результат",
                "Вход UTC",
                "Секунд от начала",
                "ASK входа",
                "Импульс входа",
            ]
        )
        self.history.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.diagnostics = table(
            ["Монета", "Секция", "Фильтр", "Значения", "Причина ожидания / действие"]
        )
        self.sections = table(
            ["Монета", "Секция", "Циклы", "Выигрышные", "Net PnL", "Макс. DCA"]
        )
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        for widget, title in [
            (self.positions, "Позиции"),
            (self.orders, "Ордера"),
            (self.history, "История"),
            (self.diagnostics, "Сигналы"),
            (self.sections, "Статистика секций"),
            (self.logs, "Журнал"),
        ]:
            self.tabs.addTab(widget, title)
        chart_page = QWidget()
        chart_layout = QVBoxLayout(chart_page)
        self.chart_symbol = QComboBox()
        chart_layout.addWidget(self.chart_symbol)
        self.chart = CandleChart()
        chart_layout.addWidget(self.chart, 1)
        self.chart_symbol.currentTextChanged.connect(self.refresh_chart)
        self.tabs.addTab(chart_page, "График")
        self.tabs.setMinimumHeight(160)
        splitter.addWidget(self.tabs)
        splitter.setSizes([320, 440])
        workspace = QSplitter(Qt.Orientation.Horizontal)
        self.bot_tree = QTreeWidget()
        self.bot_tree.setObjectName("botsTree")
        self.bot_tree.setHeaderLabel("MY BOTS")
        self.bot_tree.setMinimumWidth(180)
        self.bot_tree.setMaximumWidth(380)
        self.bot_tree.itemSelectionChanged.connect(self.tree_selected)
        self.bot_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.bot_tree.customContextMenuRequested.connect(self.tree_context_menu)
        workspace.addWidget(self.bot_tree)
        workspace.addWidget(splitter)
        workspace.setStretchFactor(0, 0)
        workspace.setStretchFactor(1, 1)
        workspace.setSizes([230, 1200])
        layout.addWidget(workspace, 1)
        footer = QHBoxLayout()
        self.notice = label("", "muted")
        self.notice.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum
        )
        self.notice.setMaximumHeight(90)
        footer.addWidget(self.notice, 1)
        self.close_positions_button = button(
            "Закрыть PAPER-позиции", self.close_positions
        )
        footer.addWidget(self.close_positions_button)
        footer.addWidget(button("Экспорт истории CSV", self.export_history))
        layout.addLayout(footer)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(self.preferences.get("refresh_ms", 2000))
        # Process reconciliation belongs exclusively to the independent runtime
        # service. This window only submits explicit desired-state commands.
        self.refresh()
        self.update_strategy_navigation()

    def update_strategy_navigation(self, *args):
        current = self.filter.currentData()
        for strategy, control in self.strategy_buttons.items():
            control.setChecked(current == strategy)

    def select_strategy(self, s):
        if s:
            self.filter.setCurrentIndex(self.filter.findData(s))

    def show_running_bots(self):
        from .monitoring import RunningBotsWindow

        if self.running_monitor is None:
            self.running_monitor = RunningBotsWindow(
                self.repo, self.life, self, self.preferences.get("refresh_ms", 2000)
            )
            self.running_monitor.open_bot.connect(self.open_from_monitor)
        self.running_monitor.show()
        self.running_monitor.raise_()
        self.running_monitor.activateWindow()

    def open_from_monitor(self, path):
        folder = self.repo.resolve(path)
        self.search.clear()
        self.filter.setCurrentIndex(0)
        self.current = folder
        self.refresh()
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()

    def selected(self):
        if self.current is None:
            raise ValueError("Выберите бота")
        return self.repo.resolve(self.current)

    def execute(self, fn):
        try:
            fn()
            self.refresh()
        except Exception as e:
            QMessageBox.warning(self, "Действие не выполнено", str(e))

    def refresh(self, *args):
        service = observe_runtime(self.repo.root)
        service_description = (
            "Служба: работает" if service is not None
            else "Служба: недоступна — команды ожидают исполнения"
        )
        # Durable observation, not a guess based on the Qt button click.
        # Query only the selected bot; never scan every command each refresh.
        if self.current is not None:
            try:
                key = self.supervisor._key(self.current)
                command = self.supervisor.coordinator.command_status(key)
                labels = {
                    'queued': 'в очереди', 'running': 'исполняется',
                    'applied': 'выполнена', 'error': 'ошибка исполнения',
                    'unknown': 'нет подтверждения',
                }
                service_description += (
                    f" · Команда #{command['revision']}: "
                    + labels.get(command['state'], command['state'])
                )
                if command['state'] == 'error':
                    service_description += ' · ' + command['error'][:150]
            except (OSError, ValueError, KeyError):
                # The current bot can be deleted between selection and refresh.
                pass
        self.runtime_status.setText(service_description)
        current = self.current
        selected = self.filter.currentData()
        query = self.search.text().casefold()
        rows = []
        self.rows = []
        total_cash = 0
        count = 0
        self.title.setText(STRATEGY_NAMES.get(selected, "Все боты"))
        inventory = self.repo.list()
        for folder, cfg in inventory:
            if selected and cfg["strategy"] != selected:
                continue
            if (
                query
                and query
                not in (cfg["name"] + " " + ",".join(cfg.get("symbols", []))).casefold()
            ):
                continue
            try:
                status = self.life.status(folder)
                if status == "Остановлен":
                    last_error = self.life.last_error(folder)
                    if last_error:
                        status = "Остановлен с ошибкой: " + last_error
                elif status == "Работает":
                    from .market_health import display_market_status
                    try:
                        import json
                        process = json.loads((folder / "process.json").read_text(encoding="utf-8"))
                    except (AttributeError, OSError, ValueError):
                        process = None
                    status = display_market_status(status, process)
                if cfg.get("invalid"):
                    raise ValueError(cfg["invalid"])
                state = (
                    Store(folder).read(include_charts=False)
                    if (folder / "paper.sqlite").exists()
                    else None
                )
                cash = state["cash"] if state else cfg["capital"]
                positions = len(state["positions"]) if state else 0
                pnl = state.get("realized", 0) if state else 0
                total_cash += cash
                count += positions
                row = [
                    cfg["name"],
                    STRATEGY_NAMES[cfg["strategy"]],
                    ", ".join(cfg["symbols"]),
                    status,
                    f"{cash:.2f}",
                    positions,
                    f"{pnl:+.2f}",
                ]
            except Exception as e:
                row = [
                    cfg["name"],
                    cfg["strategy"],
                    "—",
                    "Ошибка: " + str(e),
                    "—",
                    "—",
                    "—",
                ]
            self.rows.append((folder, cfg))
            rows.append(row)
        self.bots_table.blockSignals(True)
        populate(self.bots_table, rows, [str(folder) for folder, _ in self.rows])
        for i in range(self.bots_table.rowCount()):
            if self.bots_table.item(i, 0).data(Qt.ItemDataRole.UserRole) == str(
                current
            ):
                self.bots_table.selectRow(i)
        self.bots_table.blockSignals(False)
        if current and not any(folder == current for folder, _ in self.rows):
            self.current = None
        self.render_tree(inventory)
        self.metrics.setText(
            f"Ботов: {len(rows)}     Открытых позиций: {count}     Денежный баланс: {total_cash:,.2f} USDT"
        )
        self.refresh_details()

    def selection(self):
        row = self.bots_table.currentRow()
        if row >= 0:
            item = self.bots_table.item(row, 0)
            path = item.data(Qt.ItemDataRole.UserRole) if item else None
            self.current = Path(path) if path else None
        self.refresh_details()

    def render_tree(self, inventory):
        expanded = {
            self.bot_tree.topLevelItem(i)
            .data(0, Qt.ItemDataRole.UserRole + 1): self.bot_tree.topLevelItem(i)
            .isExpanded()
            for i in range(self.bot_tree.topLevelItemCount())
        }
        self.bot_tree.blockSignals(True)
        self.bot_tree.clear()
        query = self.search.text().casefold()
        groups = {}
        for folder, cfg in inventory:
            if (
                query
                and query
                not in (cfg["name"] + " " + ",".join(cfg.get("symbols", []))).casefold()
            ):
                continue
            groups.setdefault(cfg["strategy"], []).append((folder, cfg))
        for strategy, records in sorted(groups.items()):
            parent = QTreeWidgetItem(
                [f"{STRATEGY_NAMES.get(strategy, strategy)} ({len(records)})"]
            )
            parent.setData(0, Qt.ItemDataRole.UserRole + 1, strategy)
            self.bot_tree.addTopLevelItem(parent)
            parent.setExpanded(expanded.get(strategy, True))
            for folder, cfg in sorted(
                records, key=lambda row: row[1]["name"].casefold()
            ):
                item = QTreeWidgetItem([cfg["name"]])
                item.setData(0, Qt.ItemDataRole.UserRole, str(folder))
                item.setData(0, Qt.ItemDataRole.UserRole + 1, strategy)
                parent.addChild(item)
                if folder == self.current:
                    self.bot_tree.setCurrentItem(item)
        self.bot_tree.blockSignals(False)

    def tree_selected(self):
        item = self.bot_tree.currentItem()
        if item is None:
            return
        path = item.data(0, Qt.ItemDataRole.UserRole)
        strategy = item.data(0, Qt.ItemDataRole.UserRole + 1)
        self.current = Path(path) if path else None
        index = self.filter.findData(strategy)
        if index != self.filter.currentIndex():
            self.filter.setCurrentIndex(index)
        else:
            self.refresh()

    def tree_context_menu(self, point):
        item = self.bot_tree.itemAt(point)
        if item is None or not item.data(0, Qt.ItemDataRole.UserRole):
            return
        self.bot_tree.setCurrentItem(item)
        self._show_bot_menu(self.bot_tree.viewport().mapToGlobal(point))

    def refresh_details(self):
        tables = (
            self.positions,
            self.orders,
            self.history,
            self.diagnostics,
            self.sections,
        )
        if self.current != getattr(self, "_details_folder", None) or not self.current:
            for t in tables:
                t.setRowCount(0)
            self.logs.clear()
        self._details_folder = self.current
        self.history_events = []
        self.monitor_state = None
        self.chart.set_data("", [])
        self.close_positions_button.setEnabled(self.current is not None)
        if not self.current:
            self.notice.setText(
                "Выберите бота для просмотра позиций, сигналов и истории."
            )
            return
        try:
            folder = self.selected()
            cfg, _ = self.repo.load(folder)
            self.close_positions_button.setVisible(cfg["strategy"] != "POLYMARKET")
            if not (folder / "paper.sqlite").exists():
                for t in tables:
                    t.setRowCount(0)
                self.logs.clear()
                self.notice.setText(
                    "Бот создан. PAPER-счёт будет открыт при первом запуске."
                )
                return
            store = Store(folder)
            state = store.read(include_charts=False)
            self.monitor_state = state
            chosen = self.chart_symbol.currentText()
            symbols = store.chart_symbols()
            self.chart_symbol.blockSignals(True)
            self.chart_symbol.clear()
            self.chart_symbol.addItems(symbols)
            if chosen in symbols:
                self.chart_symbol.setCurrentText(chosen)
            self.chart_symbol.blockSignals(False)
            self.refresh_chart()
            now = time.time()
            pos = []
            pos_keys = []
            orders = []
            order_keys = []
            for key, p in state["positions"].items():
                value = position_mark_pnl(state, p, now)
                upnl = "— (нет свежей оценки)" if value is None else f"{value:+.4f}"
                initial_margin = position_initial_margin(state, p)
                pos.append(
                    [
                        p["symbol"],
                        p["section"],
                        p["side"],
                        f"{p['qty']:.8g}",
                        f"{p['average']:.8g}",
                        f"{initial_margin:.2f}" if initial_margin is not None else "—",
                        upnl,
                        p.get("dca_fills", 0),
                        time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(p["opened"])),
                    ]
                )
                pos_keys.append(key)
                if p.get("kind") == "POLYMARKET":
                    continue
                partial = p["settings"]["orders"]["partial_count"]
                for i, order in enumerate(
                    p["orders"][p["next_order"] :], p["next_order"]
                ):
                    orders.append(
                        [
                            p["symbol"],
                            p["section"],
                            i + 1,
                            f"{order['price']:.8g}",
                            f"{order['weight'] * 100:.3g}",
                            (
                                "Активен"
                                if i < p["next_order"] + partial
                                else "Ожидает очереди"
                            ),
                        ]
                    )
                    order_keys.append(f"{key}:{i}")
            for key, p in state["pending"].items():
                orders.append(
                    [
                        p["symbol"],
                        key,
                        1,
                        f"{p['orders'][0]['price']:.8g}",
                        "—",
                        "Ожидание первого исполнения",
                    ]
                )
                order_keys.append(key + ":pending")
            populate(self.positions, pos, pos_keys)
            populate(self.orders, orders, order_keys)
            events = store.history()
            self.history_events = events
            populate(
                self.history,
                [
                    [
                        time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(e["at"])),
                        e["symbol"],
                        e["section"],
                        e["action"],
                        f"{e.get('price', 0):.8g}",
                        f"{e.get('qty', 0):.8g}",
                        f"{e.get('net_pnl', 0):+.4f}",
                        e.get("reason", ""),
                        e.get("result", ""),
                        e.get(
                            "entry_ts_utc",
                            e.get("entry_evidence", {}).get("entry_ts_utc", ""),
                        ),
                        e.get(
                            "seconds_into_market",
                            e.get("entry_evidence", {}).get("seconds_into_market", ""),
                        ),
                        e.get(
                            "entry_ask",
                            e.get("entry_evidence", {}).get("entry_ask", ""),
                        ),
                        e.get(
                            "entry_impulse",
                            e.get("entry_evidence", {}).get("entry_impulse", ""),
                        ),
                    ]
                    for e in events
                ],
                [e["id"] for e in events],
            )
            diagnostic_rows = []
            for sym, ds in state["diagnostics"].items():
                for d in ds:
                    diagnostic_rows.append(
                        [sym, d.get("section", ""), "—", "—", d["reason"]]
                    )
                    for c in d.get("conditions", []):
                        values = (
                            f"{c['value']:.6g} {c.get('comparison', '')} {c.get('reference', 0):.6g}"
                            if c.get("available")
                            else "—"
                        )
                        diagnostic_rows.append(
                            [
                                sym,
                                d.get("section", ""),
                                f"{c['indicator']} / {c['timeframe']}m",
                                values,
                                (
                                    "Выполнено"
                                    if c["matched"]
                                    else (
                                        "Ожидание"
                                        if c.get("available")
                                        else "Недостаточно данных"
                                    )
                                ),
                            ]
                        )
            populate(self.diagnostics, diagnostic_rows)
            populate(
                self.sections,
                [
                    [
                        key.split(":")[0],
                        ":".join(key.split(":")[1:]),
                        v["cycles"],
                        v["wins"],
                        f"{v['net_pnl']:+.4f}",
                        v.get("max_dca", 0),
                    ]
                    for key, v in state["stats"].items()
                ],
                list(state["stats"]),
            )
            log_parts = []
            for name in ("engine.log", "console.log"):
                log = folder / name
                if log.exists():
                    with log.open("rb") as stream:
                        stream.seek(max(0, log.stat().st_size - 64000))
                        part = stream.read().decode("utf-8", errors="replace")
                    if part:
                        log_parts.append(name + "\n" + part)
            text = "\n\n".join(log_parts) or "Журнал пуст"
            if text != self.logs.toPlainText():
                bar = self.logs.verticalScrollBar()
                old_position = bar.value()
                at_end = old_position >= bar.maximum()
                self.logs.setPlainText(text)
                bar.setValue(bar.maximum() if at_end else old_position)
            stale = [
                s for s, q in state["marks"].items() if not quote_is_recent(q, now=now)
            ]
            free_margin = available_margin(state)
            free_margin_text = f"{free_margin:.2f} USDT" if math.isfinite(free_margin) else "— (нет достоверной оценки)"
            self.notice.setText(
                f"{cfg['name']} · свободная маржа {free_margin_text} · сохранено позиций {len(pos)}"
                + f" · капитал {'—' if equity(state)[1] else format(equity(state)[0], '.2f')} · макс. DD {state.get('max_drawdown', 0):.2f}%"
                + (
                    f" · реинвестировано {state['strategy'].get('veles_reinvested', 0):.2f} USDT"
                    if cfg["strategy"] == "VELES"
                    else ""
                )
                + (
                    f" · устаревших котировок: {len(stale)}; подробности — «Сигналы»"
                    if stale
                    else ""
                )
            )
        except Exception as e:
            for t in tables:
                t.setRowCount(0)
            self.monitor_state = None
            self.logs.clear()
            self.notice.setText("Ошибка чтения состояния: " + str(e))

    def refresh_chart(self, *args):
        symbol = self.chart_symbol.currentText()
        bars = []
        if symbol and self.current:
            try:
                bars = Store(self.selected()).chart(symbol)
            except (ValueError, OSError, RuntimeError) as exc:
                self.notice.setText("Ошибка чтения графика: " + str(exc))
        self.chart.set_data(symbol, bars)

    def create_bot(self):
        selected = self.filter.currentData()
        if selected is None:
            from .strategy_picker import StrategyPicker

            picker = StrategyPicker(self)
            if picker.exec() != QDialog.DialogCode.Accepted:
                return
            selected = picker.selected_strategy
        cfg = defaults(selected)
        draft = self.repo.root / ("draft_" + selected + ".json")
        revision = None
        if draft.exists():
            resume = QMessageBox.question(
                self, "Черновик", "Продолжить сохранённый черновик?")
            if resume == QMessageBox.StandardButton.Yes:
                try:
                    from .constructor_session import load_constructor_draft_snapshot
                    cfg, revision = load_constructor_draft_snapshot(
                        draft, expected_strategy=selected)
                except (ValueError, OSError, TypeError, KeyError) as exc:
                    QMessageBox.warning(self, "Черновик не открыт", str(exc))
                    return
            else:
                # Starting a second bot must not overwrite the first draft.
                from uuid import uuid4
                draft = self.repo.root / f"draft_{selected}_{uuid4().hex}.json"

        wizard = BotWizard(cfg, self, draft_path=draft,
                           draft_revision=revision, on_save=self._created)
        if wizard.exec() == QDialog.DialogCode.Accepted:
            self._preserve_constructor_workspace(
                self.current, wizard, owned_draft_path=draft)
            self.refresh()

    def _preserve_constructor_workspace(self, folder, wizard, *, owned_draft_path=None):
        """One non-trading post-commit handler for Create/Edit/Open Draft.

        Imported JSON documents are not owned drafts. Their source path is
        deliberately excluded from cleanup, even when it was edited in Qt.
        """
        try:
            from .constructor_edit_workspace import preserve_published_constructor_workspace
            status = preserve_published_constructor_workspace(
                self.repo, folder, wizard.result_workspace, wizard.result_config,
                owned_draft_path=owned_draft_path,
                owned_draft_revision=(wizard.draft_revision if owned_draft_path is not None else None))
            if status['dormant_preserved']:
                self.notice.setText("Бот сохранён; отключённые настройки сохранены в черновике")
            elif status['old_draft_removed'] is False:
                self.notice.setText("Бот сохранён; черновик другого окна не удалён")
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self.notice.setText(f"Бот сохранён, но перенос черновика не выполнен: {exc}")

    def _created(self, cfg):
        """Publish a new bot exactly once; also used by imported draft flows."""
        self.current = self.supervisor.create_bot(cfg)
        return True

    def edit(self, *args):
        def action():
            path = self.selected()
            if self.life.status(path) != "Остановлен":
                raise ValueError("Остановите бота перед редактированием")
            cfg, digest = self.repo.load(path)
            from .constructor_edit_workspace import edit_workspace_path, edit_workspace_binding
            from .constructor_session import load_constructor_draft_snapshot
            edit_draft = edit_workspace_path(self.repo.root, path, digest)
            edit_binding = edit_workspace_binding(self.repo.root, path, digest)
            edit_revision = None
            editing = cfg
            if edit_draft.exists():
                resume = QMessageBox.question(
                    self, "Черновик редактирования",
                    "Продолжить несохранённые изменения этого бота? "
                    "Черновик относится только к текущей версии его конфигурации.")
                if resume == QMessageBox.StandardButton.Yes:
                    # A corrupt or mismatched workspace must remain untouched;
                    # never silently replace it with defaults or another bot.
                    editing, edit_revision = load_constructor_draft_snapshot(
                        edit_draft, expected_strategy=cfg["strategy"],
                        binding=edit_binding, allow_legacy=True)
                else:
                    # The existing workspace belongs to another editing
                    # session. Open the current config without a draft-write
                    # target: do not overwrite or orphan either session.
                    edit_draft = None

            def save_edited(proposed):
                from .inspection import changes

                diff = changes(cfg, proposed)
                if not diff:
                    return True
                report = QPlainTextEdit()
                report.setReadOnly(True)
                review = "\n\n".join(diff)
                if cfg["strategy"] in ("VELES", "POLYMARKET"):
                    from .constructor_review import constructor_change_review_text
                    # The raw field diff alone hides inherited market/section
                    # changes. Show the effective worker rules in the SAME
                    # final confirmation, before any repository mutation.
                    review = constructor_change_review_text(cfg, proposed) + "\n\n" + review
                report.setPlainText(review)
                if not confirm_dialog(report, "Изменения перед сохранением", self):
                    return False
                # Repository.save checks the original digest and process/engine
                # locks. Conflict/error leaves this very wizard open for retry.
                self.supervisor.save_bot(path, proposed, digest)
                return True

            wizard = BotWizard(
                editing, self, draft_path=edit_draft,
                draft_revision=edit_revision, draft_binding=edit_binding,
                on_save=save_edited)
            if wizard.exec() == QDialog.DialogCode.Accepted:
                self._preserve_constructor_workspace(
                    path, wizard, owned_draft_path=edit_draft)



        self.execute(action)

    def view_settings(self, *args):
        def action():
            from .inspection import inspect_settings

            path = self.selected()
            documents, source = self.settings_documents(path)
            inspect_settings(
                self, documents, source, refresh=lambda: self.settings_documents(path)
            )

        self.execute(action)

    def settings_documents(self, path):
        path = self.repo.resolve(path)
        cfg, digest = self.repo.load(path)
        source = "Конфигурация на диске"
        documents = {"На диске": cfg}
        process = self.life.active_snapshot(path)
        if process:
            documents["Действует в процессе"] = process["active_config"]
            source = "Действующие настройки подтверждённого процесса"
            if process["config_hash"] != digest:
                source += " · файл на диске отличается"
        elif (path / "process.json").exists():
            previous = load_json(path / "process.json")
            if isinstance(previous.get("active_config"), dict):
                documents["Последний запуск"] = previous["active_config"]
            if previous.get("status") == "ERROR":
                documents["Последняя ошибка запуска"] = {
                    "reason": previous.get("last_error", "Причина не записана"),
                    "at": previous.get("heartbeat"),
                }
            source += " · действующего процесса нет"
        if (path / "paper.sqlite").exists():
            state = Store(path).read(include_charts=False)
            documents["Управление выполнением"] = {
                "draining": state["strategy"].get("draining", False),
                "finish_on_close": state["strategy"].get("finish_on_close", False),
            }
            documents["Открытые позиции"] = {
                key: {
                    "settings": p.get("settings", {}),
                    "strategy_parameters": p.get("strategy_params", {}),
                    "exit": p.get("exit_signal", {}),
                    "reinvest_pct": p.get("reinvest_pct", 0),
                    "entry_rule": p.get("entry_rule", {}),
                    "entry_observation": p.get("entry_observation", {}),
                    "margin_evidence": p.get("margin_evidence", {}),
                    "margin_data_error": p.get("margin_data_error", ""),
                    "liquidation": p.get("liquidation", {}),
                    "liquidation_error": p.get("liquidation_error", ""),
                    "last_liquidation": p.get("last_liquidation", {}),
                }
                for key, p in state["positions"].items()
            }
            if state.get("margin_model"):
                documents["Финансовая модель"] = {
                    "margin_model": state["margin_model"],
                    "margin_data": state.get("margin_data", {}),
                }
            documents["Ожидающие сетки"] = state["pending"]
            if cfg["strategy"] == "VELES":
                documents["Реинвестирование счёта"] = {
                    "capital": cfg["capital"],
                    "veles_reinvested": state["strategy"].get("veles_reinvested", 0),
                    "cash": state["cash"],
                }
        return documents, source

    def _supervise(self, running, force=False):
        folder = self.selected()
        result = self.supervisor.submit(folder, running=running, force=force)
        if result.error:
            raise RuntimeError(result.error)
        # This is a queue acknowledgement, never a claim that the worker
        # completed its transition. Show service availability separately.
        self.notice.setText(
            "Команда сохранена; ожидает исполнения управляющей службой"
        )
        return result

    def start(self):
        self.execute(lambda: self._supervise(True))

    def stop(self):
        self.execute(lambda: self._supervise(False))

    def finish(self):
        self.execute(lambda: self.supervisor.submit_drain(self.selected(), draining=True))

    def cancel_finish(self):
        self.execute(lambda: self.supervisor.submit_drain(self.selected(), draining=False))

    def force(self):
        if (
            QMessageBox.question(
                self,
                "Принудительная остановка",
                "Завершить процесс выбранного бота? Последняя завершённая транзакция и позиции сохранятся.",
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.execute(lambda: self._supervise(False, force=True))

    def close_positions(self):
        if (
            QMessageBox.question(
                self,
                "Закрытие PAPER",
                "Закрыть все позиции выбранного бота по свежим котировкам и отменить ожидающие сетки?",
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.execute(lambda: self.supervisor.submit_manual_close(self.selected()))

    def copy_bot(self):
        answer = QMessageBox.question(
            self,
            "Копирование",
            "Перенести историю, баланс и открытые PAPER-позиции в независимую копию?",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No
            | QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Cancel:
            return
        self.execute(
            lambda: setattr(
                self,
                "current",
                self.supervisor.copy_bot(
                    self.selected(), history=answer == QMessageBox.StandardButton.Yes
                ),
            )
        )

    def delete(self):
        if not self.current:
            return
        try:
            folder = self.selected()
            cfg, _ = self.repo.load(folder)
            impact = self.repo.deletion_impact(folder)
        except Exception as exc:
            QMessageBox.warning(self, "Удаление бота", str(exc))
            return

        warning = (
            f"Удалить бота «{cfg['name']}» вместе с конфигурацией, "
            "PAPER-счётом, открытыми позициями, заявками, историей и файлами?\n\n"
        )
        if impact["unknown"]:
            warning += (
                "ВНИМАНИЕ: PAPER-счёт отсутствует или не читается. "
                "Состав данных невозможно подтвердить.\n\n"
            )
        elif impact["positions"] or impact["pending"]:
            warning += (
                f"ВНИМАНИЕ: открытых PAPER-позиций: {impact['positions']}; "
                f"ожидающих заявок: {impact['pending']}. "
                "Они будут удалены без закрывающих сделок.\n\n"
            )
        warning += (
            "Удаление безвозвратно. Остановка сохраняет состояние, "
            "удаление уничтожает его. Работающий бот удалять нельзя.\n\n"
            "Подтвердить безвозвратное удаление?"
        )
        answer = QMessageBox.warning(
            self,
            "Безвозвратное удаление бота",
            warning,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.execute(lambda: self.supervisor.delete_bot(folder, discard_paper_state=True))

    def context_menu(self, point):
        index = self.bots_table.indexAt(point)
        if not index.isValid():
            return
        self.bots_table.selectRow(index.row())
        self._show_bot_menu(self.bots_table.viewport().mapToGlobal(point))

    def _show_bot_menu(self, global_point):
        menu = QMenu(self)
        for title, fn in [
            ("Запуск", self.start),
            ("Стоп", self.stop),
            ("Завершить после текущих сделок", self.finish),
            ("Отменить завершение", self.cancel_finish),
            ("Принудительная остановка", self.force),
            ("Просмотреть настройки", self.view_settings),
            ("Редактировать", self.edit),
            ("Копировать", self.copy_bot),
            ("Удалить", self.delete),
        ]:
            menu.addAction(title, fn)
        menu.exec(global_point)

    def save_current_report(self, *, choose_root=False):
        root = self.repo.root
        if choose_root or not self.repo.list():
            selected = QFileDialog.getExistingDirectory(
                self,
                "Папка программы с MY_BOTS — отчёт без остановки",
                str(root.parent),
            )
            if not selected:
                return
            root = Path(selected).resolve()
        filename = (
            "BCC_CURRENT_REPORT_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".zip"
        )
        path, _ = QFileDialog.getSaveFileName(
            self, "Отчёт текущих ботов без остановки", filename, "ZIP (*.zip)"
        )
        if not path:
            return

        def action():
            from .support import export_current_report

            summary = export_current_report(
                root, path, life=self.life if root == self.repo.root else None
            )
            QMessageBox.information(
                self,
                "Отчёт сохранён",
                f"Ботов в отчёте: {summary['bot_count']}. Работа процессов не изменена.\n{path}",
            )

        self.execute(action)

    def transfer_menu(self):
        menu = QMenu(self)
        menu.addAction("Экспорт выбранного бота", lambda: self.export_bots(False))
        menu.addAction("Экспорт всех ботов", lambda: self.export_bots(True))
        menu.addAction("Импорт независимых копий", self.import_bots)
        menu.addAction("Открыть черновик бота", self.open_draft)
        menu.addAction("Отчёт текущих ботов без остановки", self.save_current_report)
        menu.addAction(
            "Отчёт другой папки без остановки",
            lambda: self.save_current_report(choose_root=True),
        )
        menu.exec(self.cursor().pos())

    def open_draft(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Открыть черновик", "", "JSON (*.json)"
        )
        if not path:
            return

        def action():
            from .constructor_session import load_constructor_draft_snapshot

            # Edit workspaces are tied to a particular bot and config revision.
            # Never import them as an unrelated new bot through Open Draft.
            if Path(path).name.startswith("draft_edit_"):
                raise ValueError(
                    "Это черновик редактирования существующего бота. "
                    "Откройте исходного бота через «Редактировать».")
            cfg, revision = load_constructor_draft_snapshot(path)
            wizard = BotWizard(
                cfg, self, draft_path=Path(path), draft_revision=revision,
                on_save=self._created
            )
            if wizard.exec() == QDialog.DialogCode.Accepted:
                # The selected JSON is an external source, never a cleanup
                # target. Disabled policies are migrated to the new bot's
                # own digest-bound edit draft, just as in normal Create.
                self._preserve_constructor_workspace(self.current, wizard)
                self.refresh()


        self.execute(action)

    def export_bots(self, all_bots):
        history = self.transfer_history("Состав экспорта")
        if history is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Перенос ботов", "Bots_Backup.zip", "ZIP (*.zip)"
        )
        if path:
            self.execute(
                lambda: self.supervisor.export_bots(
                    [p for p, c in self.repo.list()] if all_bots else [self.selected()],
                    path,
                    history=history,
                )
            )

    def import_bots(self):
        path, _ = QFileDialog.getOpenFileName(self, "Импорт ботов", "", "ZIP (*.zip)")
        if path:
            history = self.transfer_history("Состав импорта")
            if history is not None:
                self.execute(lambda: self.supervisor.import_bots(path, history=history))

    def transfer_history(self, title):
        options = [
            "Настройки, баланс, позиции, статистика и журналы",
            "Только настройки — начать с новым PAPER-счётом",
        ]
        value, ok = QInputDialog.getItem(
            self, title, "Что переносить", options, editable=False
        )
        return value == options[0] if ok else None

    def export_history(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "История", "trades.csv", "CSV (*.csv)"
        )
        if path:

            self.execute(lambda: self.supervisor.export_history_csv(self.selected(), path))

    def shared_risk_settings(self):
        def action():
            from .shared_risk import load_policy, portfolio_snapshot

            editor = Fields(
                load_policy(self.repo.root),
                [
                    (
                        "max_positions",
                        "Позиции и ожидающие сетки: общий лимит (0 — без лимита)",
                        "int",
                        (0, 1_000_000),
                    ),
                    (
                        "max_committed_usdt",
                        "Обеспечение и затраты PAPER, USDT (0 — без лимита)",
                        "float",
                        (0, 1e12),
                    ),
                ],
            )
            slots, committed = portfolio_snapshot(self.repo.root)
            editor.layout().addRow(
                label(
                    f"Сейчас занято: {slots}; обеспечение и затраты: {committed:g} USDT. "
                    "Учитываются все сохранённые счета MY_BOTS, включая остановленные позиции и будущие DCA. "
                    "Лимиты проверяются совместно всеми процессами. Ранее зарезервированные сетки и выходы продолжаются. "
                    "Изменение лимитов доступно после остановки ботов."
                )
            )
            if confirm_dialog(editor, "Общий риск-менеджер", self):
                self.supervisor.save_shared_risk_policy(editor.value_data())

        self.execute(action)

    def app_settings(self):
        editor = Fields(
            self.preferences,
            [
                ("scale", "Масштаб интерфейса, %", "int", (75, 150)),
                ("refresh_ms", "Обновление мониторинга, мс", "int", (500, 30000)),
                ("theme", "Тема", "choice", [("Тёмная", "dark"), ("Светлая", "light")]),
            ],
        )
        if confirm_dialog(editor, "Настройки приложения", self):
            self.preferences = editor.value_data()
            atomic_json(self.preferences_path, self.preferences)
            self.timer.setInterval(self.preferences["refresh_ms"])
            apply_style(QApplication.instance(), self.preferences)

    def closeEvent(self, event):
        running = any(
            self.life.status(p)
            in ("Работает", "Запуск", "Остановка", "Завершает сделки")
            for p, c in self.repo.list()
        )
        if running:
            answer = QMessageBox.question(
                self,
                "Работающие боты",
                "Закрыть оболочку, оставив ботов работать? После открытия управление восстановится.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        event.accept()
        self.timer.stop()
        if self.running_monitor is not None:
            self.running_monitor.close()
        for view in self.findChildren(QTableWidget):
            view.setSortingEnabled(False)


def apply_style(app, preferences):
    app.setProperty("bccTheme", preferences.get("theme", "dark"))
    style = STYLE
    if preferences.get("theme") == "light":
        for a, b in [
            ("#101722", "#eef3f8"),
            ("#101d2c", "#e9f0f7"),
            ("#142130", "#ffffff"),
            ("#172637", "#f5f8fb"),
            ("#17283a", "#f4f7fa"),
            ("#1b2b40", "#e6edf4"),
            ("#1b2d40", "#e8eff6"),
            ("#172738", "#f2f6fa"),
            ("#e5edf8", "#1e2d40"),
            ("#f1f7ff", "#10253a"),
            ("#e6f4ff", "#173c58"),
            ("#9eb1c9", "#4e6680"),
            ("#a9c1d9", "#37526d"),
            ("#a7bdd3", "#36516c"),
        ]:
            style = style.replace(a, b)
    if preferences.get("theme") == "light":
        style += """
QDialog#botWizard {background:#edf3f9;color:#1b3044;}
QPushButton#strategyNav {background:#eff5fa;color:#35556b;border-color:#c7d8e6;}
QPushButton#strategyNav:checked {background:#dcf5f1;color:#007970;border-bottom-color:#15b0a3;}
QPushButton#strategyNav:hover {background:#e0f0f5;}

QFrame#wizardBanner,QFrame#wizardSidebar,QFrame#wizardSummary {background:#ffffff;border-color:#c8d9e7;}
QLabel#wizardHeading,QLabel#sidebarTitle,QLabel#stageHeading {color:#17314b;}
QLabel#wizardSummaryValues,QScrollArea#wizardSummaryScroll {background:#ffffff;color:#1b3044;}
QWidget#polyTabPanel {background:#f8fbfe;}
QFrame#wizardSection {background:#f6faff;border-color:#cfdeea;}
QTabWidget#polyConditionTabs::pane {background:#f8fbfe;border-color:#c8d9e7;}
QTabWidget#polyConditionTabs QTabBar::tab {background:#edf4fb;color:#36546d;border-color:#c6d6e5;}
QTabWidget#polyConditionTabs QTabBar::tab:selected {background:#daf4f2;color:#007c74;border-bottom-color:#079f91;}
QTabWidget#polyConditionTabs QTabBar::tab:hover {background:#ddecf4;}
QListWidget#steps::item {color:#496278;}
QListWidget#steps::item:hover {background:#e0f1f5;color:#16485d;}
QListWidget#steps::item:selected,QListWidget#steps::item:selected:!active {background:#d6f3ef;color:#076d66;border-left-color:#09a99c;}
QPushButton#wizardSave {background:#10b8a7;color:#ffffff;border-color:#079f91;}
QPushButton#wizardSave:hover {background:#0d9b8e;}
QPushButton#wizardDraft {background:#edf6fa;border-color:#b9d5e2;color:#204258;}
QPushButton#wizardCancel {background:#ffffff;border-color:#b8c9d8;color:#274456;}
QLabel#timingExample {color:#087c75;}
QTableWidget#polyEffectiveMarketMatrix {background:#ffffff;alternate-background-color:#f2f7fc;}
QLabel#wizardStepCounter {color:#08796e;background:#ddf5ef;border-color:#a6dbd2;}
QFrame#polyRulePreview {background:#f2fafc;border-color:#bed9e7;}
QLabel#polyRulePreviewValues {color:#19344b;}
QFrame#polyRulePreview QPushButton {background:#e6f3f7;color:#24465c;border-color:#b3d4df;}
QFrame#dashboardActionArea {background:#ffffff;border-color:#cbdce8;}
QLabel#dashboardMetrics {background:#eff8fb;border-color:#bad9e4;color:#18364b;}
QLabel#wizardStageProgress {color:#087c75;}
QLabel#wizardPaperMode {color:#08796e;background:#e2f7f2;border-color:#8dd7ca;}
QLabel#wizardSummaryHeading {color:#17314b;}
QTableWidget#polyEffectiveMarketMatrix::item:selected {background:#c6ede7;color:#143a42;}

"""
    scale = preferences.get("scale", 100)
    style = style.replace("font-size:13px", f"font-size:{13 * scale / 100:.1f}px")
    app.setStyleSheet(style)
