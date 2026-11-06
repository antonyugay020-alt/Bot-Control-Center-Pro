"""The same complete constructor is used to create, edit, and inspect bots."""

from copy import deepcopy

from PySide6.QtCore import Qt, QTimer, QSize
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QLabel,
    QListWidgetItem,
    QScrollArea,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QListWidget,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .config import (
    STRATEGY_NAMES,
    coin_section,
    defaults,
    difference,
    effective,
    merge,
    normalize_config,
    resolve_poly_market,
    validate,
    condition, group,
)
from .wizard_state import selected_configuration
from .trading import percent_stop_price, plan
from .wizard_presentation import summary_html, market_preview_html
from .wizard_stepper import WizardStepDelegate
from .widgets import (
    Fields,
    GroupEditor,
    SectionEditor,
    SectionsEditor,
    VelesSectionsEditor,
    SettingsEditor,
    button,
    combo,
    confirm_dialog,
    dialog_size,
    label,
    scroll,
    spin,
)


class BotWizard(QDialog):
    def __init__(self, cfg, parent=None, *, read_only=False, draft_path=None, on_save=None, draft_revision=None, draft_binding=None):
        super().__init__(parent)
        cfg = normalize_config(cfg)
        self.cfg = deepcopy(cfg)
        self.setObjectName("botWizard")
        self._visited_steps = set()
        self.read_only = read_only
        self.draft_path = draft_path
        self.draft_revision = draft_revision
        self.draft_binding = draft_binding
        self.on_save = on_save
        self.result_config = None
        self.result_workspace = None
        self._preflight_report = None
        self.setWindowTitle(
            ("Параметры бота" if read_only else "Конструктор бота")
            + " · "
            + STRATEGY_NAMES[cfg["strategy"]]
        )
        from PySide6.QtWidgets import QApplication

        screen = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1536, screen.width() - 30), min(940, screen.height() - 50))
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 10, 14, 10)
        root.setSpacing(9)
        banner = QFrame(self)
        banner.setObjectName("wizardBanner")
        banner_layout = QHBoxLayout(banner)
        banner_layout.setContentsMargins(17, 9, 17, 9)
        headings = QVBoxLayout()
        title = label(("Просмотр параметров" if read_only else "Создание и настройка бота")
                      + " · " + STRATEGY_NAMES[cfg["strategy"]], "headline")
        title.setObjectName("wizardHeading")
        headings.addWidget(title)
        headings.addWidget(label(
            "PAPER-торговля · данные конфигурации и фактические правила движка"
            if cfg["strategy"] == "POLYMARKET" else
            "Реальные настройки сигналов, ордеров, риска и восстановления", "muted"))
        banner_layout.addLayout(headings, 1)
        self.step_counter = label("Шаг 1 из 7", "wizardStepCounter")
        banner_layout.addWidget(self.step_counter)
        banner_layout.addWidget(label("PAPER", "pill"))
        root.addWidget(banner)
        body = QHBoxLayout()
        body.setSpacing(12)
        root.addLayout(body, 1)
        sidebar = QFrame(self)
        sidebar.setObjectName("wizardSidebar")
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(8, 12, 8, 12)
        side_layout.setSpacing(10)
        side_layout.addWidget(label("Этапы создания", "sidebarTitle"))
        self.step_note = label("Просмотрено: 0 из 7", "muted")
        self.step_note.setObjectName("wizardStageProgress")
        side_layout.addWidget(self.step_note)
        self.steps = QListWidget()
        self.steps.setObjectName("steps")
        self.steps.setFixedWidth(258)
        self.steps.setItemDelegate(WizardStepDelegate(self.steps))
        self.steps.setSpacing(0)
        self.steps.setWordWrap(True)
        self.pages = QStackedWidget()
        side_layout.addWidget(self.steps, 1)
        hint = label("Отметка этапа означает посещение, а не проверку торгового сигнала.", "muted")
        hint.setWordWrap(True)
        side_layout.addWidget(hint)
        body.addWidget(sidebar)
        body.addWidget(self.pages, 1)
        aside = QFrame(self)
        self.aside = aside
        aside.setObjectName("wizardSummary")
        aside.setFixedWidth(306)
        rail = QVBoxLayout(aside)
        rail.setContentsMargins(16, 16, 16, 16)
        summary_title = label("▣  Сводка бота", "title")
        summary_title.setObjectName("wizardSummaryHeading")
        rail.addWidget(summary_title)
        rail.addWidget(label("Значения черновика · обновляются при изменении полей", "muted"))
        tag = label("PAPER  ·  БЕЗ РЕАЛЬНЫХ ОРДЕРОВ", "pill")
        tag.setObjectName("wizardPaperMode")
        rail.addWidget(tag)
        self.summary = QLabel(aside)
        self.summary.setObjectName("wizardSummaryValues")
        self.summary.setTextFormat(Qt.TextFormat.RichText)
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.summary.setAlignment(Qt.AlignmentFlag.AlignTop)
        summary_scroll = QScrollArea()
        summary_scroll.setObjectName("wizardSummaryScroll")
        summary_scroll.setWidgetResizable(True)
        summary_scroll.setWidget(self.summary)
        rail.addWidget(summary_scroll, 1)
        rail.addWidget(label("●  Проверка условий", "title"))
        self.summary_status = label("Отдельные настройки и ограничения проверяются перед сохранением.", "muted")
        rail.addWidget(self.summary_status)
        body.addWidget(aside)
        aside.setVisible(self.width() >= 1130)
        self.settings = SettingsEditor(cfg["settings"], self, strategy=cfg["strategy"])
        self.settings.hide()
        self.settings_pages = [self.settings.widget(i) for i in range(4)]
        # Transfer complete scroll pages. Moving their content directly leaves
        # QScrollArea with a dangling owned widget in the native Qt object tree.
        while self.settings.count():
            self.settings.removeTab(0)
        for page in self.settings_pages:
            page.setParent(self)
            page.hide()
        self.sections = (VelesSectionsEditor if cfg["strategy"] == "VELES" else SectionsEditor)(
            cfg["sections"],
            lambda: self.settings.value_data(),
            strategy=cfg["strategy"],
        )
        self._basic()
        self._coins()
        self._signals()
        self._orders()
        self._exits()
        if cfg["strategy"] == "POLYMARKET":
            self._additional()
        self._review()
        # All VELES range mutations share the same effective-rule validator
        # across every selected coin. The provider reads current form state;
        # it never publishes settings or touches running bots.
        if cfg["strategy"] == "VELES":
            self.sections.config_provider = lambda: self.data(include_inactive=True)
        self.steps.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.steps.currentRowChanged.connect(self.update_summary)
        self.steps.currentRowChanged.connect(self.update_step_counter)
        self.steps.currentRowChanged.connect(self.update_stage_progress)
        self.steps.setCurrentRow(0)
        footer = QHBoxLayout()
        self.error = label("", "error")
        root.addWidget(self.error)
        cancel_button = button("Закрыть" if read_only else "Отмена", self.reject)
        cancel_button.setObjectName("wizardCancel")
        footer.addWidget(cancel_button)
        if not read_only and self.draft_path is not None:
            draft_button = button("Сохранить черновик", self.save_draft)
            draft_button.setObjectName("wizardDraft")
            footer.addWidget(draft_button)
        footer.addStretch()
        footer.addWidget(
            button(
                "← Назад",
                lambda: self.steps.setCurrentRow(max(0, self.steps.currentRow() - 1)),
            )
        )
        footer.addWidget(
            button(
                "Далее →",
                lambda: self.steps.setCurrentRow(min(self.steps.count() - 1, self.steps.currentRow() + 1)),
            )
        )
        self.save = button("Сохранить бота", self.commit, "primary")
        self.save.setObjectName("wizardSave")
        self.save.setVisible(not read_only)
        footer.addWidget(self.save)
        root.addLayout(footer)
        if read_only:
            for i in range(self.pages.count()):
                self.pages.widget(i).setEnabled(False)
        self.summary_timer = QTimer(self)
        self.summary_timer.setSingleShot(True)
        self.summary_timer.setInterval(120)
        self.summary_timer.timeout.connect(self.update_summary)
        self.finished.connect(self.summary_timer.stop)
        for kind, signal_name in (
            (QLineEdit, "textChanged"),
            (QComboBox, "currentIndexChanged"),
            (QSpinBox, "valueChanged"),
            (QDoubleSpinBox, "valueChanged"),
            (QCheckBox, "toggled"),
        ):
            for widget in self.findChildren(kind):
                getattr(widget, signal_name).connect(self.queue_summary)

    def update_stage_progress(self, row):
        """Visual visit state only: do not mislabel a visited page as valid."""
        if row < 0:
            return
        self._visited_steps.add(row)
        for number in range(self.steps.count()):
            item = self.steps.item(number)
            details = dict(item.data(Qt.ItemDataRole.UserRole) or {})
            details["visited"] = number in self._visited_steps and number != row
            item.setData(Qt.ItemDataRole.UserRole, details)
        self.step_note.setText(
            f"Просмотрено: {len(self._visited_steps)} из {self.steps.count()}")
        self.steps.viewport().update()

    def update_step_counter(self, row):
        self.step_counter.setText(f"Шаг {row + 1} из {self.steps.count()}")

    def queue_summary(self, *args):
        # Any changed form input invalidates the previous explicit preflight.
        self._preflight_report = None
        if hasattr(self, "preflight_text"):
            self.preflight_text.setText("После изменений проверьте все правила повторно.")
        # Update the visible values during the same Qt event as the edit.
        # A delayed QTimer alone leaves the summary stale after app.processEvents()
        # and is visibly confusing when changing precise seconds before close.
        # Rebuilding large market tables remains debounced separately.
        self.update_summary(lightweight=True)
        self.summary_timer.start()

    def closeEvent(self, event):
        self.summary_timer.stop()
        super().closeEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "aside"):
            self.aside.setVisible(self.width() >= 1130)

    _stage_descriptions = {
        "Основное": "Название, режим и сохранение",
        "Монеты и депозит": "Монеты и распределение капитала",
        "Условия и секции": "Сигналы, индикаторы и фильтры",
        "Условия входа": "Импульс, ASK, время и рынки",
        "Ордера и DCA": "Ордера, сетка и риск",
        "Ордера и риск": "Ставка, лимиты и комиссия",
        "Выход и защита": "Закрытие и безопасность",
        "Дополнительно": "Остановка и управление капиталом",
        "Проверка": "Итоговые параметры перед запуском",
    }

    def page(self, title):
        page = QWidget()
        page.setObjectName("wizardPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(13)
        stage_title = label(f"{self.steps.count() + 1}.  {title}", "stageHeading")
        layout.addWidget(stage_title)
        layout.addWidget(label(self._stage_descriptions.get(title, "Настройка бота"), "muted"))
        item = QListWidgetItem(
            f"{self.steps.count() + 1:02d}   {title}\n       {self._stage_descriptions.get(title, '')}"
        )
        item.setSizeHint(QSize(248, 76))
        item.setData(Qt.ItemDataRole.UserRole, {
            "title": title, "subtitle": self._stage_descriptions.get(title, ""),
            "visited": False,
        })
        self.steps.addItem(item)
        self.pages.addWidget(scroll(page))
        return layout

    def _basic(self):
        layout = self.page("Основное")
        w = QWidget()
        form = QFormLayout(w)
        self.name = QLineEdit(self.cfg["name"])
        self.name.setObjectName("botName")
        form.addRow("Название бота", self.name)
        form.addRow("Режим", label("PAPER · виртуальные сделки на публичных данных"))
        form.addRow(
            "Биржа / источник",
            label(
                "Polymarket UP/DOWN + Binance spot для импульса"
                if self.cfg["strategy"] == "POLYMARKET"
                else "Bybit · USDT Futures"
            ),
        )
        self.reinvest = QCheckBox(
            "Реинвестировать часть чистой прибыли"
            if self.cfg["strategy"] == "VELES"
            else "Размер ставки от текущего капитала"
            if self.cfg["strategy"] == "POLYMARKET"
            else "Размер позиций от текущего капитала",
            w,
        )
        self.reinvest.setChecked(self.cfg["reinvest"])
        if self.cfg["strategy"] in ("GRID", "PUMP_DUMP"):
            self.reinvest.hide()
        elif self.cfg["strategy"] != "POLYMARKET":
            form.addRow("Реинвестирование", self.reinvest)
        self.stop_after_deals = QCheckBox("Завершить работу после сделок")
        self.stop_after_deals.setChecked(self.cfg["stop_after_deals"])
        if self.cfg["strategy"] != "POLYMARKET":
            form.addRow("Завершение работы", self.stop_after_deals)
        layout.addWidget(w)
        layout.addWidget(
            label(
                "После первого завершения сделки бот отключит новые входы, дождётся остальных позиций и ожидающих сеток и остановится. При запуске с уже открытыми сделками новые входы отключаются сразу.",
                "muted",
            )
        )
        layout.addWidget(
            label(
                "Остановка сохраняет открытые PAPER-позиции, баланс и историю. После запуска бот продолжает работу с сохранённого состояния.",
                "muted",
            )
        )
        layout.addStretch()

    def _coins(self):
        layout = self.page("Монеты и депозит")
        form = QFormLayout()
        self.symbols = QLineEdit(", ".join(self.cfg["symbols"]))
        self.symbols.setObjectName("symbols")
        self.capital = spin(self.cfg["capital"], 0.01, 1e9)
        self.capital.setObjectName("capital")
        form.addRow("Монеты через запятую", self.symbols)
        form.addRow("Общий депозит бота, USDT", self.capital)
        layout.addLayout(form)
        layout.addWidget(
            button("Выбрать / добавить монеты", self.choose_coins, "secondary")
        )
        layout.addWidget(
            button(
                "Индивидуальные настройки монеты / секции", self.edit_coin, "secondary"
            )
        )
        layout.addWidget(
            label(
                "Общие настройки наследуются всеми монетами. Индивидуальные настройки можно сбросить к общим.",
                "muted",
            )
        )
        self.coin_overrides = deepcopy(self.cfg["coin_overrides"])
        self.finance_widget = self.settings.finance
        layout.addWidget(self.settings_pages[0], 1)
        self.settings_pages[0].show()

    def choose_coins(self):
        from .coin_picker import CoinPicker

        symbols = [
            s.strip().upper() for s in self.symbols.text().split(",") if s.strip()
        ]
        dialog = CoinPicker(self.cfg["strategy"], symbols, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.symbols.setText(", ".join(dialog.result_symbols))
            self.update_summary()

    def _signals(self):
        layout = self.page("Условия входа" if self.cfg["strategy"] == "POLYMARKET" else "Условия и секции")
        strategy = self.cfg["strategy"]
        if strategy in ("VELES", "EMA", "MACD", "BOLLINGER", "EMA_RSI_VOLUME"):
            layout.addWidget(self.sections)
        elif strategy == "GRID":
            self.grid = Fields(
                self.cfg["grid"],
                [
                    (
                        "side",
                        "Направление",
                        "choice",
                        [("LONG", "LONG"), ("SHORT", "SHORT")],
                    ),
                    ("levels", "Количество уровней", "int", (1, 100)),
                    ("spacing_pct", "Шаг уровней, %", "float", (0.001, 20)),
                    ("order_usdt", "Номинал ордера, USDT", "float", (0.01, 1e9)),
                    (
                        "take_profit_pct",
                        "Прибыль каждого уровня, %",
                        "float",
                        (0.001, 100),
                    ),
                    ("stop_loss_pct", "Стоп каждого уровня, %", "float", (0.001, 90)),
                ],
            )
            layout.addWidget(self.grid)
            layout.addStretch()
        elif strategy == "PUMP_DUMP":
            p = self.cfg["pump"]
            self.pump = Fields(
                p,
                [
                    ("window_minutes", "Окно импульса, минут", "int", (1, 30)),
                    ("move_pct", "Движение цены от, %", "float", (0.01, 100)),
                    ("volume_ratio", "Относительный объём от, ×", "float", (1, 1000)),
                    ("stake_usdt", "Номинал позиции, USDT", "float", (0.01, 1e9)),
                    (
                        "cooldown_seconds",
                        "Пауза после выхода, секунд",
                        "float",
                        (0, 86400),
                    ),
                ],
            )
            layout.addWidget(self.pump)
            self.pump_modes = {}
            self.pump_sides = {}
            row = QHBoxLayout()
            for key, title in [
                ("fast", "Быстрый отскок"),
                ("confirmed", "Подтверждённый отскок"),
            ]:
                c = QCheckBox(title)
                c.setChecked(key in p["modes"])
                row.addWidget(c)
                self.pump_modes[key] = c
            layout.addLayout(row)
            row = QHBoxLayout()
            for side in ("LONG", "SHORT"):
                c = QCheckBox(side)
                c.setChecked(side in p["sides"])
                row.addWidget(c)
                self.pump_sides[side] = c
            layout.addLayout(row)
            layout.addStretch()
        else:
            p = self.cfg["polymarket"]
            from .constructor_contract import POLY_FORM_SPECS, validate_poly_form_schema
            validate_poly_form_schema()
            self.poly = Fields(p, list(POLY_FORM_SPECS))
            # The original field-owner remains a child of this dialog; its
            # individual controls move into tab pages below. This avoids a
            # detached top-level QWidget surviving after the dialog closes.
            self.poly.setParent(self)
            self.poly.hide()
            # Reuse the SAME typed Fields controls read by .data() and the
            # repository/worker contract. Moving their form rows to functional
            # tabs changes presentation only: there are no duplicate inputs.
            self.poly_tabs = QTabWidget()
            self.poly_tabs.setObjectName("polyConditionTabs")
            layout.addWidget(self.poly_tabs, 1)
            specs_by_tab = [
                ("Основные условия", "Направление UP/DOWN определяется знаком импульса; здесь задаётся реальный порог входа.",
                 ("impulse_mode", "impulse")),
                ("Фильтры рынка", "Действующий коридор лучшей цены ASK и размер PAPER-ставки.",
                 ("ask_min", "ask_max", "stake_pct")),
                ("Время входа", "Точное время до закрытия свечи в секундах, не в минутах.",
                 ("entry_signal_mode", "entry_seconds_before_close",
                  "entry_window_seconds", "entry_min_seconds_left")),
                ("Дополнительно", "Комиссия по публичной формуле или собственная оценка.",
                 ("fee_mode", "fee_rate", "disabled")),
            ]
            original_form = self.poly.layout()
            original_rows = {}
            # QFormLayout.takeRow transfers each original label and input
            # without destroying them or disconnecting their signals.
            for key, *_ in self.poly.specs:
                result = original_form.takeRow(0)
                original_rows[key] = (
                    result.labelItem.widget() if result.labelItem else None,
                    result.fieldItem.widget() if result.fieldItem else None,
                )
            for title, description, keys in specs_by_tab:
                panel = QWidget()
                panel.setObjectName("polyTabPanel")
                panel_layout = QVBoxLayout(panel)
                panel_layout.setContentsMargins(12, 15, 12, 15)
                panel_layout.setSpacing(12)
                panel_layout.addWidget(label(title, "title"))
                panel_layout.addWidget(label(description, "muted"))
                content = QFrame()
                content.setObjectName("wizardSection")
                form = QFormLayout(content)
                form.setContentsMargins(18, 17, 18, 17)
                form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
                form.setSpacing(16)
                for key in keys:
                    title_label, control = original_rows[key]
                    form.addRow(title_label, control)
                panel_layout.addWidget(content)
                if title == "Основные условия":
                    self.poly_indicator_enabled = QCheckBox(
                        "Применять составные RSI / EMA / Объём условия перед PAPER-входом")
                    self.poly_indicator_enabled.setObjectName("polyIndicatorFilterEnabled")
                    current_filter = p.get("indicator_filter")
                    self.poly_indicator_enabled.setChecked(current_filter is not None)
                    panel_layout.addWidget(self.poly_indicator_enabled)
                    self.poly_indicator_editor = GroupEditor(
                        current_filter if current_filter is not None
                        else group(condition("RSI", 50, ">", 1)),
                        indicators=("RSI", "EMA", "VOLUME", "REL_VOLUME"))
                    self.poly_indicator_editor.setObjectName("polyIndicatorFilterEditor")
                    self.poly_indicator_editor.setEnabled(current_filter is not None)
                    self.poly_indicator_enabled.toggled.connect(
                        self.poly_indicator_editor.setEnabled)
                    panel_layout.addWidget(self.poly_indicator_editor)
                    rules_note = label(
                        "Сначала выполняется ценовой импульс, затем опциональный "
                        "фильтр по завершённым свечам Binance (RSI, EMA, объём). "
                        "Без достоверных свечей вход блокируется; при отключённом "
                        "фильтре действует прежняя логика.", "muted")
                    rules_note.setWordWrap(True)
                    panel_layout.addWidget(rules_note)
                    preview = QFrame()
                    preview.setObjectName("polyRulePreview")
                    preview_box = QVBoxLayout(preview)
                    preview_box.setContentsMargins(16, 14, 16, 14)
                    preview_box.addWidget(label("Проверка фактических условий PAPER-входа", "title"))
                    preview_box.addWidget(label(
                        "Применённые параметры выбранного рынка. Это проверка правил, "
                        "а не сигнал с биржи и не прогноз результата.", "muted"
                    ))
                    self.poly_rule_preview = QLabel(preview)
                    self.poly_rule_preview.setObjectName("polyRulePreviewValues")
                    self.poly_rule_preview.setTextFormat(Qt.TextFormat.RichText)
                    self.poly_rule_preview.setWordWrap(True)
                    self.poly_rule_preview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                    preview_box.addWidget(self.poly_rule_preview)
                    quick_nav = QHBoxLayout()
                    quick_nav.addWidget(button("ASK и ставка", lambda: self.open_poly_tab(1), "secondary"))
                    quick_nav.addWidget(button("Время входа", lambda: self.open_poly_tab(2), "secondary"))
                    quick_nav.addWidget(button("Параметры рынка", lambda: self.open_poly_tab(3), "secondary"))
                    preview_box.addLayout(quick_nav)
                    panel_layout.addWidget(preview)
                if "Время" in title:
                    self.timing_hint = label(
                        "Пример: 45 сек до закрытия + окно 10 сек → вход только "
                        "в интервале от 45 до 35 секунд до конца свечи.", "muted"
                    )
                    self.timing_hint.setObjectName("timingExample")
                    panel_layout.addWidget(self.timing_hint)
                    mode = self.poly.controls["entry_signal_mode"][1]
                    mode.currentIndexChanged.connect(self.refresh_timing_controls)
                    self.refresh_timing_controls()
                if title == "Фильтры рынка":
                    panel_layout.addWidget(button(
                        "Настроить ASK и ставку отдельно для монеты",
                        self.edit_coin, "secondary"
                    ))
                panel_layout.addStretch()
                self.poly_tabs.addTab(panel, title)
            markets = QWidget()
            markets.setObjectName("polyTabPanel")
            markets_layout = QVBoxLayout(markets)
            markets_layout.setContentsMargins(12, 15, 12, 15)
            markets_layout.setSpacing(12)
            markets_layout.addWidget(label("Монеты × длительности свечей", "title"))
            markets_layout.addWidget(label(
                "Выберите длительности и откройте любую строку, чтобы задать "
                "индивидуальное время, ASK и импульс этого рынка.", "muted"
            ))
            self.durations = {}
            duration_row = QHBoxLayout()
            for value, title in [(300, "5 минут"), (900, "15 минут"), (3600, "1 час")]:
                control = QCheckBox(title)
                control.setChecked(value in p["durations"])
                duration_row.addWidget(control)
                self.durations[value] = control
            duration_row.addStretch()
            markets_layout.addLayout(duration_row)
            self.poly_overrides = deepcopy(p["overrides"])
            self.poly_matrix = QTableWidget(0, 10)
            self.poly_matrix.setObjectName("polyEffectiveMarketMatrix")
            self.poly_matrix.setHorizontalHeaderLabels([
                "Монета", "Рынок, сек", "Режим", "Вход за, сек",
                "Окно, сек", "Защита, сек", "Импульс", "ASK от", "ASK до", "Статус",
            ])
            self.poly_matrix.setSelectionBehavior(
                QAbstractItemView.SelectionBehavior.SelectRows
            )
            self.poly_matrix.setSelectionMode(
                QAbstractItemView.SelectionMode.SingleSelection
            )
            self.poly_matrix.setEditTriggers(
                QAbstractItemView.EditTrigger.NoEditTriggers
            )
            self.poly_matrix.setSortingEnabled(True)
            self.poly_matrix.setMinimumHeight(260)
            self.poly_matrix.horizontalHeader().setSectionResizeMode(
                QHeaderView.ResizeMode.ResizeToContents
            )
            self.poly_matrix.horizontalHeader().setStretchLastSection(True)
            markets_layout.addWidget(self.poly_matrix, 1)
            self.poly_matrix_note = label("", "muted")
            markets_layout.addWidget(self.poly_matrix_note)
            market_actions = QHBoxLayout()
            market_actions.addWidget(button(
                "Изменить выбранный рынок", self.edit_selected_poly_market,
                "secondary",
            ))
            market_actions.addWidget(button(
                "Выбрать другой рынок", self.edit_poly_market,
            ))
            markets_layout.addLayout(market_actions)
            self.poly_matrix.cellDoubleClicked.connect(
                lambda *_: self.edit_selected_poly_market()
            )
            self.poly_matrix.itemSelectionChanged.connect(self.queue_summary)
            self.poly_tabs.insertTab(3, markets, "Рынки и монеты")
            layout.addWidget(label(
                "Все вкладки сохраняются в общей конфигурации. Конструктор и "
                "PAPER-движок используют одни и те же правила наследования: "
                "общие → монета → рынок.", "muted"
            ))

    def refresh_timing_controls(self, *args):
        if not hasattr(self, "poly"):
            return
        is_scheduled = self.poly.controls["entry_signal_mode"][1].currentData() == "before_close"
        self.poly.controls["entry_seconds_before_close"][1].setEnabled(is_scheduled and not self.read_only)
        self.poly.controls["entry_window_seconds"][1].setEnabled(is_scheduled and not self.read_only)
        if hasattr(self, "timing_hint"):
            if is_scheduled:
                fields = self.poly.value_data()
                before = fields["entry_seconds_before_close"]
                window = fields["entry_window_seconds"] or (before - fields["entry_min_seconds_left"])
                self.timing_hint.setText(
                    f"Допустимый интервал: от {before:g} до {before-window:g} "
                    "секунд до закрытия. Защитный порог действует всегда."
                )
            else:
                self.timing_hint.setText(
                    "В режиме «В любое время» секунды расписания сохранены, "
                    "но не действуют. Защитный порог обязателен."
                )

    def _orders(self):
        layout = self.page("Ордера и риск" if self.cfg["strategy"] == "POLYMARKET" else "Ордера и DCA")
        if self.cfg["strategy"] in (
            "VELES",
            "EMA",
            "MACD",
            "BOLLINGER",
            "EMA_RSI_VOLUME",
        ):
            widget = self.settings_pages[1]
            layout.addWidget(widget)
            widget.show()
        elif self.cfg["strategy"] == "POLYMARKET":
            layout.addWidget(label("Размер PAPER-ставки и оценка исполнения", "title"))
            layout.addWidget(label(
                "Polymarket использует один PAPER-вход на рынок. Размер ставки "
                "ограничивается депозитом и заданным процентом; исполнение "
                "зависит от доступного ASK и публичной формулы комиссии.", "muted"
            ))
            layout.addWidget(button(
                "Изменить ASK и размер ставки", lambda: self.open_poly_tab(1), "secondary"
            ))
            layout.addWidget(button(
                "Изменить комиссию", lambda: self.open_poly_tab(4), "secondary"
            ))
            layout.addWidget(button(
                "Индивидуальные ограничения по монетам", self.edit_coin, "secondary"
            ))
            layout.addStretch()
        else:
            layout.addWidget(label(
                "Параметры ордеров этой стратегии находятся в разделе «Условия и секции»."
            ))
            layout.addStretch()

    def open_poly_tab(self, index):
        """Navigate to the real input controls, never to a separate fake form."""
        if not hasattr(self, "poly_tabs"):
            return
        self.steps.setCurrentRow(2)
        self.poly_tabs.setCurrentIndex(index)

    def _exits(self):
        layout = self.page("Выход и защита")
        strategy = self.cfg["strategy"]
        if strategy in ("VELES", "EMA", "MACD", "BOLLINGER", "EMA_RSI_VOLUME"):
            tabs = QTabWidget()
            tabs.addTab(self.settings_pages[2], "Тейк-профит")
            tabs.addTab(self.settings_pages[3], "Стоп-лосс")
            layout.addWidget(tabs)
        elif strategy == "PUMP_DUMP":
            tabs = QTabWidget()
            self.pump_exit = {}
            for key, title in [
                ("fast", "Быстрый отскок"),
                ("confirmed", "Подтверждённый отскок"),
            ]:
                editor = Fields(
                    self.cfg["pump"][key],
                    [
                        (
                            "rebound_pct",
                            "Подтверждающий отскок, %",
                            "float",
                            (0.001, 100),
                        ),
                        ("tp_pct", "Тейк-профит, %", "float", (0.001, 10000)),
                        ("sl_pct", "Стоп-лосс, %", "float", (0.001, 90)),
                        (
                            "max_hold_seconds",
                            "Максимальное время позиции, секунд",
                            "float",
                            (1, 1e6),
                        ),
                    ],
                )
                self.pump_exit[key] = editor
                tabs.addTab(editor, title)
            layout.addWidget(tabs)
        elif strategy == "POLYMARKET":
            layout.addWidget(label("Официальный исход и защита", "title"))
            layout.addWidget(label(
                "PAPER-позиция удерживается до официального расчёта исхода. "
                "Истечение времени само по себе не означает победу или проигрыш. "
                "Повторный вход в один и тот же рынок блокируется.", "muted"
            ))
            layout.addWidget(button(
                "Настроить защитный порог до закрытия", lambda: self.open_poly_tab(2),
                "secondary",
            ))
            layout.addWidget(button(
                "Проверить доступные рынки", lambda: self.open_poly_tab(3),
            ))
            layout.addStretch()
        else:
            layout.addWidget(
                label("Тейк-профит и стоп каждого уровня указаны в настройках GRID.")
            )
            layout.addStretch()

    def _additional(self):
        """Real Polymarket settings moved here from the shared basic form."""
        layout = self.page("Дополнительно")
        layout.addWidget(label("Управление исполнением PAPER-бота", "title"))
        layout.addWidget(label(
            "Эти переключатели сохраняются в конфигурации и обрабатываются "
            "движком. Они не меняют существующие PAPER-позиции при остановке.",
            "muted",
        ))
        panel = QFrame()
        panel.setObjectName("wizardSection")
        controls = QVBoxLayout(panel)
        controls.setContentsMargins(20, 18, 20, 18)
        controls.addWidget(self.reinvest)
        controls.addWidget(self.stop_after_deals)
        layout.addWidget(panel)
        layout.addWidget(label(
            "Остановка сохраняет счёт, открытые позиции, ожидающие расчёты "
            "и журнал исполнения. Для перезапуска не требуется создавать "
            "бота заново.", "muted"
        ))
        layout.addStretch()

    def _review(self):
        layout = self.page("Проверка")
        self.review = label("")
        layout.addWidget(self.review)
        self.validate_all_button = button(
            "Проверить все монеты, диапазоны и рынки",
            self.run_constructor_preflight, "secondary")
        self.validate_all_button.setObjectName("wizardValidateAll")
        layout.addWidget(self.validate_all_button)
        self.preflight_text = label(
            "Проверка всех действующих правил не запускалась.", "muted")
        self.preflight_text.setObjectName("wizardPreflightResults")
        self.preflight_text.setWordWrap(True)
        layout.addWidget(self.preflight_text)
        self.preview_selectors = QWidget()
        selection = QHBoxLayout(self.preview_selectors)
        self.preview_coin = combo([(s, s) for s in self.cfg["symbols"]])
        self.preview_section = combo(
            [(s["label"], s["id"]) for s in self.cfg["sections"]]
        )
        for selector in (self.preview_coin, self.preview_section):
            selector.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            selector.setMinimumContentsLength(5)
        selection.addWidget(label("Монета"))
        selection.addWidget(self.preview_coin, 1)
        selection.addWidget(label("Секция"))
        selection.addWidget(self.preview_section, 1)
        layout.addWidget(self.preview_selectors)
        self.preview = QTableWidget(0, 4)
        self.preview.setHorizontalHeaderLabels(
            ["Ордер", "Условная цена", "Объём, %", "Номинал, USDT"]
        )
        self.preview.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self.preview)
        # The effective-change review and the conditional order-grid preview
        # are independent outputs.  Changing the selected coin/section must
        # never replace the before/after VELES or Polymarket change audit.
        self.preview_status = label("", "muted")
        self.preview_status.setObjectName("wizardGridPreviewStatus")
        self.preview_status.setWordWrap(True)
        layout.addWidget(self.preview_status)
        self.preview_note = label(
            "Расчёт сетки показан относительно условной цены 100. Исполнение PAPER использует текущие котировки, заданное проскальзывание и комиссии.",
            "muted",
        )
        layout.addWidget(self.preview_note)
        self.preview_coin.currentIndexChanged.connect(self.update_summary)
        self.preview_section.currentIndexChanged.connect(self.update_summary)

    def run_constructor_preflight(self):
        """Diagnose every selected rule without publishing or starting a bot."""
        from .constructor_diagnostics import constructor_preflight
        try:
            report = constructor_preflight(self.data(include_inactive=True))
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            report = {'valid': False, 'checked_rules': 0,
                      'errors': [{'scope': 'Форма', 'message': str(exc)}],
                      'truncated': False, 'dormant_excluded': []}
        self._preflight_report = report
        if report["valid"]:
            message = (f"✓ Проверено {report['checked_rules']} действующих правил. "
                       "Конфигурация проходит проверку сохранения и загрузки.")
        else:
            details = [f"{item['scope']}: {item['message']}"
                       for item in report['errors'][:12]]
            message = (f"⚠ Проверено {report['checked_rules']} правил; "
                       f"обнаружено ошибок: {len(report['errors'])}.\n" +
                       "\n".join(details))
            if report['truncated'] or len(report['errors']) > 12:
                message += "\nИсправьте показанные ошибки и повторите проверку."
        if report['dormant_excluded']:
            message += (f"\nНеактивные правила не публикуются: "
                        f"{len(report['dormant_excluded'])}.")
        self.preflight_text.setText(message)
        return report

    def data(self, *, include_inactive=False):
        """Build the constructor workspace or the publishable bot config.

        Drafts must retain overrides for temporarily unchecked coins, VELES
        sections and Polymarket durations. Only repository commits, effective
        previews and validation project to selected combinations. Persisting
        the projected view as a draft silently lost these user edits.
        """
        cfg = deepcopy(self.cfg)
        cfg.update(
            name=self.name.text().strip(),
            symbols=[
                s.strip().upper() for s in self.symbols.text().split(",") if s.strip()
            ],
            capital=self.capital.value(),
            reinvest=self.reinvest.isChecked(),
            stop_after_deals=self.stop_after_deals.isChecked(),
            settings=self.settings.value_data(),
            sections=self.sections.value_data(),
            coin_overrides=deepcopy(self.coin_overrides),
        )
        if hasattr(self, "grid"):
            cfg["grid"] = self.grid.value_data()
        if hasattr(self, "pump"):
            cfg["pump"] = self.pump.value_data()
            cfg["pump"]["modes"] = [
                k for k, c in self.pump_modes.items() if c.isChecked()
            ]
            cfg["pump"]["sides"] = [
                k for k, c in self.pump_sides.items() if c.isChecked()
            ]
            for k, v in self.pump_exit.items():
                cfg["pump"][k] = v.value_data()
        if hasattr(self, "poly"):
            cfg["polymarket"] = self.poly.value_data()
            cfg["polymarket"]["indicator_filter"] = (
                self.poly_indicator_editor.value_data()
                if self.poly_indicator_enabled.isChecked() else None)
            cfg["polymarket"]["durations"] = [
                k for k, c in self.durations.items() if c.isChecked()
            ]
            cfg["polymarket"]["overrides"] = deepcopy(self.poly_overrides)
        # Draft files store the complete editor workspace. The repository
        # must receive only selected/active combinations; no dormant rule
        # can be sent to a worker or used as a live trading setting.
        if include_inactive:
            return cfg
        projected, self.dropped_overrides = selected_configuration(cfg)
        return projected

    def update_summary(self, *args, lightweight=False):
        try:
            c = self.data()
            s = c["settings"]
            symbols = ", ".join(c["symbols"][:6])
            if len(c["symbols"]) > 6:
                symbols += f" и ещё {len(c['symbols']) - 6}"
            kind = c["strategy"]
            has_grid = kind not in ("POLYMARKET", "GRID", "PUMP_DUMP")
            if kind == "POLYMARKET" and not lightweight:
                self.refresh_poly_matrix(c)
            from PySide6.QtWidgets import QApplication
            is_light = QApplication.instance().property("bccTheme") == "light"
            self.summary.setText(summary_html(c, light=is_light))
            if kind == "POLYMARKET":
                selected_symbol, selected_duration = None, None
                if self.poly_matrix.currentRow() >= 0:
                    item = self.poly_matrix.item(self.poly_matrix.currentRow(), 0)
                    if item is not None:
                        market_key = item.data(Qt.ItemDataRole.UserRole + 1)
                        if isinstance(market_key, str) and ":" in market_key:
                            selected_symbol, duration_text = market_key.rsplit(":", 1)
                            try:
                                selected_duration = int(duration_text)
                            except ValueError:
                                pass
                self.poly_rule_preview.setText(market_preview_html(
                    c, selected_symbol, selected_duration, light=is_light
                ))
                self.refresh_timing_controls()
            if hasattr(self, "summary_status"):
                if not lightweight:
                    try:
                        validate(c)
                    except (ValueError, KeyError, TypeError) as error:
                        self.summary_status.setText("⚠  Исправьте параметры: " + str(error))
                    else:
                        self.summary_status.setText(
                            "✓  Правила допустимы. Это черновик — сохранение отдельно."
                        )
            self.preview_selectors.setVisible(has_grid)
            self.preview.setVisible(has_grid)
            self.preview_note.setVisible(has_grid)
            self.preview_status.setVisible(has_grid)
            review_text = (
                "Проверьте параметры и сохраните бота. Он появится в MY BOTS "
                "и будет готов к отдельному запуску."
            )
            if getattr(self, "dropped_overrides", None):
                review_text += "\nПри сохранении будут исключены настройки удалённых "                                "монет, рынков и секций: " + ", ".join(
                                   self.dropped_overrides[:8])
                if len(self.dropped_overrides) > 8:
                    review_text += f" (и ещё {len(self.dropped_overrides) - 8})"
            if (kind in ("VELES", "POLYMARKET") and not lightweight
                    and self.steps.currentRow() == self.steps.count() - 1):
                # A passive effective-rule diff: inherited per-coin/market
                # timing and VELES signals are reviewed before the user saves.
                # This never applies a configuration or accesses a worker.
                from .constructor_review import constructor_change_review_text
                review_text += "\n\n" + constructor_change_review_text(self.cfg, c)
            if has_grid and not lightweight:
                self.update_grid_preview(c)
                # Review the *effective* selected coin/section risk, not only
                # global defaults.  The grid and SL shown here are computed
                # by the same plan/effective policy used in the preview.
                if self.preview.rowCount():
                    review_text += "\n\n" + self.preview_status.text()
            self.review.setText(review_text)
        except (ValueError, KeyError, TypeError) as e:
            self.summary.setText("Не удалось сформировать сводку: " + str(e))
            if hasattr(self, "summary_status"):
                self.summary_status.setText("●  Есть ошибка в заполненных полях")

    def update_grid_preview(self, cfg):
        def choices(selector, items):
            if items == [
                (selector.itemText(i), selector.itemData(i))
                for i in range(selector.count())
            ]:
                return
            previous = selector.currentData()
            blocked = selector.blockSignals(True)
            selector.clear()
            for title, data in items:
                selector.addItem(title, data)
            index = selector.findData(previous)
            selector.setCurrentIndex(index if index >= 0 else 0)
            selector.blockSignals(blocked)

        choices(self.preview_coin, [(s, s) for s in cfg["symbols"]])
        choices(self.preview_section, [(s["label"], s["id"]) for s in cfg["sections"]])
        symbol = self.preview_coin.currentData()
        original = next(
            (
                s
                for s in cfg["sections"]
                if s["id"] == self.preview_section.currentData()
            ),
            None,
        )
        if not symbol or original is None:
            self.preview.setRowCount(0)
            self.preview_status.setText("Выберите монету и добавьте секцию для расчёта сетки")
            return
        section = coin_section(cfg, symbol, original)
        settings = effective(cfg, symbol, original)
        rows = plan(settings["orders"], section["side"], 100)
        margin = cfg["capital"] * settings["allocation_pct"] / 100
        budget = margin * settings["leverage"]
        self.preview.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for j, value in enumerate(
                (
                    str(i + 1),
                    f"{row['price']:.8g}",
                    f"{row['weight'] * 100:.5g}",
                    f"{budget * row['weight']:.4f}",
                )
            ):
                self.preview.setItem(i, j, QTableWidgetItem(value))
        text = f"{symbol} · {section['label']} · {section['side']}\nМаржа из исходного депозита: {margin:g} USDT; номинал сетки с плечом: {budget:g} USDT."
        if settings["stop"]["mode"] in ("percent", "both"):
            if settings["orders"]["mode"] == "signal":
                text += "\nSL рассчитывается после фактического последнего усреднения."
            else:
                stop = percent_stop_price(
                    {"settings": settings, "side": section["side"], "orders": rows}
                )
                text += f"\nУсловный SL: {stop:.8g} после исполнения сетки."
        if not section["enabled"] or settings["disabled"]:
            text += "\nНовые входы этой секции отключены."
        self.preview_status.setText(text)

    def edit_coin(self):
        symbols = [
            s.strip().upper() for s in self.symbols.text().split(",") if s.strip()
        ]
        if not symbols:
            return
        chooser = QDialog(self)
        chooser.setWindowTitle("Индивидуальные настройки")
        dialog_size(chooser, 1100, 800)
        layout = QVBoxLayout(chooser)
        coin = combo([(s, s) for s in symbols])
        coin.setObjectName("coinChoice")
        layout.addWidget(coin)
        tabs = QTabWidget()
        tabs.setObjectName("coinSettingsTabs")
        layout.addWidget(tabs)
        state = {"symbol": None}
        draft = deepcopy(self.coin_overrides)

        def save_current():
            if state["symbol"] is None:
                return
            if individual.isChecked():
                values = deepcopy(draft.get(state["symbol"], {}))
                values.update(
                    {
                        "settings": difference(
                            self.settings.value_data(), editor.value_data()
                        ),
                        "sections": deepcopy(state.get("section_overrides", {})),
                    }
                )
                if state.get("parameters_editor"):
                    params = state["parameters_editor"].value_data()
                    for key, child in state.get("exit_editors", {}).items():
                        params[key] = child.value_data()
                    if state.get("poly_coin_filter") is not None:
                        enable_filter, filter_editor = state["poly_coin_filter"]
                        params["indicator_filter"] = (
                            filter_editor.value_data() if enable_filter.isChecked() else None)
                    if self.cfg["strategy"] == "POLYMARKET":
                        from .config import difference_poly_rule
                        values["parameters"] = difference_poly_rule(state["base_parameters"], params)
                    else:
                        values["parameters"] = difference(state["base_parameters"], params)
                draft[state["symbol"]] = values
            else:
                draft.pop(state["symbol"], None)

        individual = QCheckBox("Индивидуальные параметры монеты")
        individual.setObjectName("coinIndividual")
        layout.addWidget(individual)
        editor = None

        def load():
            nonlocal editor
            save_current()
            sym = coin.currentData()
            state["symbol"] = sym
            over = draft.get(sym, {})
            while tabs.count():
                old = tabs.widget(0)
                tabs.removeTab(0)
                old.deleteLater()
            editor = SettingsEditor(
                merge(self.settings.value_data(), over.get("settings", {})),
                strategy=self.cfg["strategy"],
            )
            tabs.addTab(editor, "Настройки монеты")
            individual.setChecked(bool(over))
            tabs.setEnabled(individual.isChecked())
            state["section_overrides"] = deepcopy(over.get("sections", {}))
            state["parameters_editor"] = None
            state["exit_editors"] = {}
            state["poly_coin_filter"] = None
            block = {"GRID": "grid", "PUMP_DUMP": "pump", "POLYMARKET": "poly"}.get(
                self.cfg["strategy"]
            )
            if block:
                main = getattr(self, block)
                params = main.value_data()
                if self.cfg["strategy"] == "POLYMARKET":
                    params["indicator_filter"] = (
                        self.poly_indicator_editor.value_data()
                        if self.poly_indicator_enabled.isChecked() else None)
                state["base_parameters"] = deepcopy(params)
                if self.cfg["strategy"] == "POLYMARKET":
                    from .config import merge_poly_rule
                    values = merge_poly_rule(params, over.get("parameters", {}))
                else:
                    values = merge(params, over.get("parameters", {}))
                local = Fields(values, main.specs)
                state["parameters_editor"] = local
                if self.cfg["strategy"] == "POLYMARKET":
                    current_filter = values.get("indicator_filter")
                    enable_filter = QCheckBox("Индикаторные фильтры этой монеты")
                    enable_filter.setChecked(current_filter is not None)
                    filter_editor = GroupEditor(
                        current_filter if current_filter is not None
                        else group(condition("RSI", 50, ">", 1)),
                        indicators=("RSI", "EMA", "VOLUME", "REL_VOLUME"))
                    filter_editor.setEnabled(current_filter is not None)
                    enable_filter.toggled.connect(filter_editor.setEnabled)
                    enable_filter.toggled.connect(lambda _checked: individual.setChecked(True))
                    local.layout().addRow(enable_filter)
                    local.layout().addRow(filter_editor)
                    state["poly_coin_filter"] = (enable_filter, filter_editor)
                tabs.addTab(scroll(local), "Параметры стратегии")
                for key, source in getattr(self, "pump_exit", {}).items():
                    child = Fields(values[key], source.specs)
                    state["exit_editors"][key] = child
                    tabs.addTab(scroll(child), key)
            section_page = QWidget()
            v = QVBoxLayout(section_page)
            selection = combo(
                [(s["label"], s["id"]) for s in self.sections.value_data()]
            )
            selection.setObjectName("coinSectionChoice")
            v.addWidget(selection)

            def change_section():
                sec = next(
                    s
                    for s in self.sections.value_data()
                    if s["id"] == selection.currentData()
                )
                patch = state["section_overrides"].get(sec["id"], {})
                current_coin = deepcopy(draft.get(sym, {}))
                current_coin["sections"] = state["section_overrides"]
                local = coin_section({"coin_overrides": {sym: current_coin}}, sym, sec)
                local["overrides"] = patch.get("settings", {})
                child = SectionEditor(
                    local,
                    merge(editor.value_data(), sec.get("overrides", {})),
                    strategy=self.cfg["strategy"],
                )
                if confirm_dialog(
                    child, "Настройки " + sym + " / " + sec["label"], chooser
                ):
                    changed = child.value_data()
                    changes = {}
                    if changed["overrides"]:
                        changes["settings"] = changed["overrides"]
                    for key in ("entry", "exit", "side", "enabled", "label"):
                        inherited = (
                            current_coin.get(key, sec[key])
                            if key in ("entry", "exit")
                            else sec[key]
                        )
                        if changed[key] != inherited:
                            changes[key] = changed[key]
                    if changes:
                        state["section_overrides"][sec["id"]] = changes
                    else:
                        state["section_overrides"].pop(sec["id"], None)
                    individual.setChecked(True)

            edit_section = button("Настроить секцию этой монеты", change_section)
            edit_section.setObjectName("editCoinSection")
            v.addWidget(edit_section)
            v.addWidget(
                button(
                    "Сбросить секции к общим",
                    lambda: state.update(section_overrides={}),
                )
            )
            v.addStretch()
            if self.cfg["strategy"] not in ("POLYMARKET", "GRID", "PUMP_DUMP"):
                tabs.addTab(section_page, "Диапазоны и сигналы")
            else:
                section_page.deleteLater()

        coin.currentIndexChanged.connect(load)
        individual.toggled.connect(tabs.setEnabled)
        load()

        def apply_to(targets):
            save_current()
            for target in targets:
                if state["symbol"] in draft:
                    draft[target] = deepcopy(draft[state["symbol"]])
                else:
                    draft.pop(target, None)

        def apply_selected():
            selection = QListWidget()
            selection.addItems(symbols)
            selection.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
            if confirm_dialog(
                selection, "Применить параметры к выбранным монетам", chooser
            ):
                apply_to([item.text() for item in selection.selectedItems()])

        layout.addWidget(
            button("Применить текущие параметры к выбранным монетам", apply_selected)
        )
        layout.addWidget(
            button(
                "Применить текущие параметры ко всем монетам", lambda: apply_to(symbols)
            )
        )
        controls = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        layout.addWidget(controls)
        coin_error = label("", "error")
        layout.insertWidget(layout.indexOf(controls), coin_error)

        def accept_coin():
            # A coin edit affects *all* of its markets, including market
            # overrides already present in the wizard. Validate the complete
            # inherited policy before closing, not just the active coin tab.
            # Do not change self.coin_overrides until the dialog is accepted.
            try:
                save_current()
                if self.cfg["strategy"] in ("POLYMARKET", "VELES"):
                    from .constructor_contract import project_constructor_coin_transaction
                    current = self.data()
                    changes = {
                        symbol: deepcopy(draft.get(symbol)) if symbol in draft else None
                        for symbol in symbols
                        if draft.get(symbol) != self.coin_overrides.get(symbol)
                    }
                    # Stage the COMPLETE coin records: risk/sections as well as
                    # inherited market timing, ASK, impulse and filter policy.
                    # Invalid bulk edits leave the dialog and draft untouched.
                    project_constructor_coin_transaction(current, coin_rules=changes)
            except (ValueError, KeyError, TypeError) as exc:
                coin_error.setText("Не удалось применить настройки монеты: " + str(exc))
                return
            chooser.accept()

        controls.accepted.connect(accept_coin)
        controls.rejected.connect(chooser.reject)
        if chooser.exec() == QDialog.DialogCode.Accepted:
            self.coin_overrides = draft
            self.update_summary()
        chooser.deleteLater()

    def refresh_poly_matrix(self, cfg):
        """Keep market identities, user sorting and selection across draft edits.

        Changing a field in the wizard updates the *same* live market matrix;
        it must not rebuild rows in config order after a user has sorted by
        duration or ASK.  Qt's native QVariant sort is retained deliberately:
        custom QTableWidgetItem.__lt__ caused a Windows/PySide6 crash.
        """
        if not hasattr(self, "poly_matrix"):
            return
        matrix = self.poly_matrix
        selected = None
        row = matrix.currentRow()
        if row >= 0 and matrix.item(row, 0):
            selected = matrix.item(row, 0).data(Qt.ItemDataRole.UserRole + 1)

        from .market_selection import market_preview
        # Only the visible rows are materialized.  All other combinations
        # remain editable via the independent coin/duration selector.
        preview, total_markets = market_preview(cfg, limit=300)
        rows = {}
        status_notes = {}
        from .config import validate_poly_rule
        for symbol, duration in preview:
            rule = resolve_poly_market(cfg, symbol, duration)
            try:
                validate_poly_rule(rule, duration)
                status = "Выключен" if rule["disabled"] else "PAPER"
            except (KeyError, TypeError, ValueError) as exc:
                status = "⚠ Ошибка"
                status_notes[f"{symbol}:{duration}"] = str(exc)
            rows[f"{symbol}:{duration}"] = (
                symbol, duration,
                "За N секунд" if rule["entry_signal_mode"] == "before_close"
                else "В любое время",
                rule["entry_seconds_before_close"],
                rule["entry_window_seconds"],
                rule["entry_min_seconds_left"],
                rule["impulse"], rule["ask_min"], rule["ask_max"],
                status,
            )
        signature = tuple((key, values, status_notes.get(key))
                          for key, values in rows.items())
        if (getattr(self, "_poly_matrix_signature", None) == signature
                and matrix.rowCount() == len(rows)):
            # Nothing effective changed. Do not ask Qt to sort equal cells again
            # or move the selection while the user edits an unrelated field.
            return

        existing = []
        for row in range(matrix.rowCount()):
            item = matrix.item(row, 0)
            if item is not None:
                key = item.data(Qt.ItemDataRole.UserRole + 1)
                if key in rows and key not in existing:
                    existing.append(key)
        # Preserve the current *visual order* of retained markets, including
        # equal sort keys. New markets append and are sorted by Qt afterwards.
        ordered = existing + [key for key in rows if key not in existing]
        original_block = matrix.blockSignals(True)
        matrix.setSortingEnabled(False)
        try:
            matrix.setRowCount(len(ordered))
            for row, key in enumerate(ordered):
                for col, value in enumerate(rows[key]):
                    item = matrix.item(row, col)
                    if item is None or type(item) is not QTableWidgetItem:
                        item = QTableWidgetItem()
                        matrix.setItem(row, col, item)
                    if type(value) in (int, float):
                        item.setData(Qt.ItemDataRole.DisplayRole, value)
                        item.setTextAlignment(
                            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                        )
                    else:
                        item.setText(str(value))
                    item.setData(Qt.ItemDataRole.UserRole + 1, key)
                    if col == 9:
                        item.setToolTip(status_notes.get(key, ""))
        finally:
            matrix.setSortingEnabled(True)
            matrix.blockSignals(original_block)

        self._poly_matrix_signature = signature
        matrix.setVisible(bool(preview))
        self.poly_matrix_note.setText(
            f"Показано {len(preview)} из {total_markets} рынков; остальные доступны "
            "через выбор рынка." if len(preview) < total_markets else
            f"Индивидуальные параметры: {total_markets} комбинаций монет и рынков"
        )
        # Qt can move rows after sorting numeric values. Recover the selected
        # market by immutable symbol:duration, never by its old row number.
        if selected in rows:
            for row in range(matrix.rowCount()):
                item = matrix.item(row, 0)
                if item and item.data(Qt.ItemDataRole.UserRole + 1) == selected:
                    matrix.selectRow(row)
                    break

    def edit_selected_poly_market(self):
        row = self.poly_matrix.currentRow()
        if row < 0 or self.poly_matrix.item(row, 0) is None:
            self.edit_poly_market()
            return
        selected = self.poly_matrix.item(row, 0).data(Qt.ItemDataRole.UserRole + 1)
        self.edit_poly_market(selected)

    def edit_poly_market(self, selected=None):
        cfg = self.data()
        from .market_selection import market_axes, market_key
        from PySide6.QtWidgets import QInputDialog
        symbols, durations = market_axes(cfg)
        if selected is not None:
            try:
                symbol, duration_text = selected.rsplit(":", 1)
                duration = int(duration_text)
                selected = market_key(cfg, symbol, duration)
            except (ValueError, AttributeError):
                selected = None
        if selected is None:
            # A single list of every coin × duration can freeze Qt and hide
            # markets outside the bounded preview.  Two small independent
            # selectors cover the complete matrix without an artificial cap.
            symbol, ok = QInputDialog.getItem(
                self, "Монета рынка", "Выберите монету", symbols, editable=True)
            if not ok:
                return
            # Accept a complete market key from keyboard/older Qt pickers,
            # while retaining independent coin/duration selectors for large
            # matrices.  Never silently reinterpret an invalid market key.
            if ':' in symbol:
                try:
                    direct_coin, direct_seconds = symbol.rsplit(':', 1)
                    direct_duration = int(direct_seconds)
                except ValueError:
                    direct_coin, direct_duration = None, None
                if direct_coin not in symbols or direct_duration not in durations:
                    self.error.setText("Рынок отсутствует в конструкторе")
                    return
                selected = market_key(cfg, direct_coin, direct_duration)
            else:
                if symbol not in symbols:
                    self.error.setText("Монета отсутствует в конструкторе")
                    return
                duration_text, ok = QInputDialog.getItem(
                    self, "Длительность рынка", "Выберите длительность в секундах",
                    [str(value) for value in durations], editable=False)
                if not ok:
                    return
                selected = market_key(cfg, symbol, int(duration_text))
        symbol, duration_text = selected.rsplit(":", 1)
        duration = int(duration_text)
        # Both the typed controls and the separate indicator GroupEditor must
        # reopen the same effective policy. Fields.value_data() intentionally
        # excludes indicator_filter; reading it there would erase an existing
        # inherited or market-specific filter on an unrelated edit.
        from .constructor_contract import market_editor_rules
        base, current_rule = market_editor_rules(cfg, symbol, int(duration))
        specs = list(self.poly.specs)
        fields = Fields(current_rule, specs)
        filter_holder = QWidget()
        filter_layout = QVBoxLayout(filter_holder)
        filter_layout.setContentsMargins(0, 0, 0, 0)
        current = current_rule.get("indicator_filter")
        filter_checkbox = QCheckBox("Индикаторные фильтры только этого рынка")
        filter_checkbox.setChecked(current is not None)
        filter_editor = GroupEditor(
            current if current is not None else group(condition("RSI", 50, ">", 1)),
            indicators=("RSI", "EMA", "VOLUME", "REL_VOLUME"))
        filter_editor.setEnabled(current is not None)
        filter_checkbox.toggled.connect(lambda checked: filter_editor.setEnabled(checked))
        filter_layout.addWidget(filter_checkbox)
        filter_layout.addWidget(filter_editor)
        fields.layout().addRow(filter_holder)

        def market_values():
            values = fields.value_data()
            values["indicator_filter"] = (
                filter_editor.value_data() if filter_checkbox.isChecked() else None)
            return values

        def reset_market():
            fields.set_values({k: base[k] for k, *_ in specs})
            # The filter builder stays visible for review. Reset its group by
            # replacing the editor itself, not by retaining a stale override.
            nonlocal filter_editor
            filter_layout.removeWidget(filter_editor)
            filter_editor.deleteLater()
            original = base.get("indicator_filter")
            filter_editor = GroupEditor(
                original if original is not None else group(condition("RSI", 50, ">", 1)),
                indicators=("RSI", "EMA", "VOLUME", "REL_VOLUME"))
            filter_layout.addWidget(filter_editor)
            filter_checkbox.setChecked(original is not None)
            filter_editor.setEnabled(original is not None)

        fields.layout().addRow(button(
            "Сбросить комбинацию к параметрам монеты", reset_market))

        from .constructor_contract import validate_market_override

        def validate_market():
            # Validate against the current draft, including inherited coin
            # rules.  Unfinished unrelated pages are checked on final Save.
            validate_market_override(cfg, symbol, int(duration), market_values())

        if confirm_dialog(
            fields, "Настройки " + selected, self, validator=validate_market
        ):
            patch = validate_market_override(cfg, symbol, int(duration), market_values())
            if patch:
                self.poly_overrides[selected] = patch
            else:
                self.poly_overrides.pop(selected, None)
            self.update_summary()

    def save_draft(self):
        if self.draft_path:
            try:
                from .constructor_session import save_constructor_draft_with_revision
                _, self.draft_revision = save_constructor_draft_with_revision(
                    self.draft_path, self.data(include_inactive=True),
                    expected_revision=self.draft_revision,
                    binding=self.draft_binding, allow_legacy=True)
                self.error.setText("Черновик сохранён и проверен повторной загрузкой")
            except (OSError, ValueError, TypeError, KeyError) as e:
                self.error.setText(str(e))

    def commit(self):
        """Keep the editor open until the repository actually commits the bot.

        The caller owns the create/edit transaction. In particular, an OS lock,
        optimistic config conflict, or disk error must never discard an edited
        form or turn an unsuccessful save into an Accepted dialog.
        """
        self.result_config = None
        self.result_workspace = None
        try:
            from .constructor_transaction import publish_wizard_configuration
            # All strategy families use one publication gateway. VELES and
            # Polymarket additionally verify inherited effective policies;
            # other strategies use their normal strict config contract.
            workspace = self.data(include_inactive=True)
            proposed = publish_wizard_configuration(workspace)
        except (ValueError, KeyError, TypeError) as exc:
            self.error.setText(str(exc))
            return
        try:
            # A return value of False means the user cancelled the change
            # review; no repository mutation should have happened.
            if self.on_save is not None and self.on_save(proposed) is False:
                self.error.setText("Сохранение не подтверждено. Настройки остаются в редакторе.")
                return
        except Exception as exc:
            # Show the underlying failure and preserve ALL unsaved widget data.
            # For a new bot also retain its validated draft for crash recovery,
            # independently of whether the native dialog can be kept open.
            note = ""
            if self.draft_path is not None:
                try:
                    from .constructor_session import save_constructor_draft_with_revision
                    _, self.draft_revision = save_constructor_draft_with_revision(
                        self.draft_path, self.data(include_inactive=True),
                        expected_revision=self.draft_revision,
                        binding=self.draft_binding, allow_legacy=True)
                    note = " · черновик сохранён"
                except (OSError, ValueError):
                    note = " · черновик не перезаписан (возможно, изменён другим окном)"
            self.error.setText(f"Не удалось сохранить: {exc}{note}")
            return
        self.result_config = proposed
        self.result_workspace = deepcopy(workspace)
        self.error.clear()
        self.accept()
