"""Modern Qt editors. Every visible input serializes into the worker contract."""

import uuid
from copy import deepcopy

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .config import (
    TIMEFRAMES,
    condition,
    difference,
    group,
    merge,
    range_section,
)
from .indicators import CATALOG, EXTRA, PERIODLESS


def label(value, kind=""):
    w = QLabel(value)
    w.setObjectName(kind)
    w.setWordWrap(kind != "headline")
    return w


def button(text, fn=None, kind=""):
    b = QPushButton(text)
    b.setObjectName(kind)
    if fn:
        b.clicked.connect(fn)
    return b


def combo(items, value=None):
    w = QComboBox()
    for text, data in items:
        w.addItem(text, data)
    idx = w.findData(value)
    if idx >= 0:
        w.setCurrentIndex(idx)
    return w


def spin(value, lo=-1e12, hi=1e12, integer=False):
    w = QSpinBox() if integer else QDoubleSpinBox()
    if not integer:
        w.setDecimals(8)
    w.setRange(lo, hi)
    w.setValue(value)
    w.setKeyboardTracking(False)
    return w


def scroll(widget):
    s = QScrollArea()
    s.setWidgetResizable(True)
    s.setFrameShape(QFrame.Shape.NoFrame)
    widget.setProperty("scrollContent", True)
    s.setWidget(widget)
    return s


def dialog_size(widget, width, height):
    available = QApplication.primaryScreen().availableGeometry()
    widget.resize(
        min(width, available.width() - 40), min(height, available.height() - 60)
    )


def confirm_dialog(widget, title, parent=None, *, validator=None):
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog_size(dialog, 1050, 750)
    layout = QVBoxLayout(dialog)
    container = scroll(widget)
    layout.addWidget(container)
    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
    )

    def accept():
        try:
            if hasattr(widget, "validate_data"):
                widget.validate_data()
            if validator:
                validator()
        except (ValueError, KeyError, TypeError) as e:
            QMessageBox.warning(dialog, "Исправьте параметры", str(e))
            return
        dialog.accept()

    buttons.accepted.connect(accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    accepted = dialog.exec() == QDialog.DialogCode.Accepted
    # The caller reads the editor after this function returns. Release it from
    # QScrollArea ownership before disposing of the temporary native dialog.
    buttons.accepted.disconnect(accept)
    container.takeWidget()
    widget.setParent(None)
    dialog.deleteLater()
    return accepted


class ConditionRow(QFrame):
    removed = Signal(object)

    def __init__(self, data, parent=None, *, indicators=None):
        super().__init__(parent)
        self.setObjectName("condition")
        self.data = deepcopy(data)
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        bottom = QHBoxLayout()
        choices = [(name, key) for key, name in CATALOG.items()
                   if indicators is None or key in indicators]
        self.ind = combo(choices, data["indicator"])
        self.ind.setMinimumWidth(180)
        self.period = spin(data["period"], 2, 500, True)
        self.tf = combo([(f"{m} мин", m) for m in TIMEFRAMES], data["timeframe"])
        self.trigger = combo(
            [("На закрытии бара", "bar_close"), ("Внутри свечи", "intrabar")],
            data["trigger"],
        )
        self.shift = spin(data.get("shift", 0), 0, 500, True)
        self.period_label = None
        for title, widget in [
            ("Индикатор", self.ind),
            ("Период", self.period),
            ("Интервал", self.tf),
            ("Сигнал", self.trigger),
            ("Сдвиг", self.shift),
        ]:
            title_label = label(title, "muted")
            col = QVBoxLayout()
            col.addWidget(title_label)
            col.addWidget(widget)
            top.addLayout(col)
            if widget is self.period:
                self.period_label = title_label
        top.addWidget(button("⚙", self.params_dialog))
        top.addWidget(button("×", lambda: self.removed.emit(self)))
        self.op = combo(
            [
                ("Больше", ">"),
                ("Больше или равно", ">="),
                ("Меньше", "<"),
                ("Меньше или равно", "<="),
                ("Пересекает вверх", "cross_up"),
                ("Пересекает вниз", "cross_down"),
            ],
            data["comparison"],
        )
        self.reference = combo(
            [("Число", "number"), ("Цена", "price"), ("Индикатор", "indicator")],
            data["reference"],
        )
        self.value = spin(data["value"])
        self.ref_ind = combo(
            choices, data.get("reference_indicator", data["indicator"]),
        )
        self.ref_period = spin(data.get("reference_period", 26), 2, 500, True)
        self.ref_tf = combo(
            [(f"{m} мин", m) for m in TIMEFRAMES],
            data.get("reference_timeframe", data["timeframe"]),
        )
        self.ref_shift = spin(data.get("reference_shift", 0), 0, 500, True)
        for w in (self.op, self.reference, self.value):
            bottom.addWidget(w)
        self.reference_panel = QWidget()
        refs = QHBoxLayout(self.reference_panel)
        refs.setContentsMargins(0, 0, 0, 0)
        for w in (self.ref_ind, self.ref_period, self.ref_tf, self.ref_shift):
            refs.addWidget(w)
        refs.addWidget(button("⚙", self.reference_params_dialog))
        self.ref_period.setToolTip("Период второго индикатора")
        self.ref_shift.setToolTip("Сдвиг второго индикатора")
        layout.addLayout(top)
        layout.addLayout(bottom)
        layout.addWidget(self.reference_panel)
        self.reference.currentIndexChanged.connect(self.visibility)
        self.ind.currentIndexChanged.connect(self.indicator_changed)
        self.ref_ind.currentIndexChanged.connect(self.reference_changed)
        self.visibility()
        self.period_visibility()

    def period_visibility(self):
        self.period.setVisible(self.ind.currentData() not in PERIODLESS)
        self.period_label.setVisible(self.ind.currentData() not in PERIODLESS)
        self.ref_period.setVisible(
            self.reference.currentData() == "indicator"
            and self.ref_ind.currentData() not in PERIODLESS
        )

    def indicator_changed(self):
        self.data["params"] = {}
        self.period_visibility()

    def reference_changed(self):
        self.data.pop("reference_params", None)
        self.period_visibility()

    def visibility(self):
        ref = self.reference.currentData()
        self.value.setVisible(ref == "number")
        self.reference_panel.setVisible(ref == "indicator")
        for w in (self.ref_ind, self.ref_period, self.ref_tf, self.ref_shift):
            w.setVisible(ref == "indicator")
        self.period_visibility()

    def params_dialog(self):
        self.edit_params(self.ind.currentData(), "params")

    def reference_params_dialog(self):
        self.edit_params(self.ref_ind.currentData(), "reference_params")

    def edit_params(self, name, data_key):
        spec = EXTRA.get(name, {})
        if not spec:
            QMessageBox.information(
                self,
                "Параметры",
                "Для этого индикатора доступны период, интервал и сдвиг в строке фильтра.",
            )
            return
        container = QWidget()
        form = QFormLayout(container)
        controls = {}
        for k, v in spec.items():
            value = self.data.get(data_key, {}).get(k, v)
            w = spin(
                value,
                0 if k == "offset" else 1 if type(v) is int else 0.001,
                1 if k == "offset" else 500,
                type(v) is int,
            )
            form.addRow(k, w)
            controls[k] = w
        if confirm_dialog(container, "Дополнительные параметры", self):
            self.data[data_key] = {k: w.value() for k, w in controls.items()}

    def value_data(self):
        d = dict(self.data)
        d.update(
            indicator=self.ind.currentData(),
            period=self.period.value(),
            timeframe=self.tf.currentData(),
            trigger=self.trigger.currentData(),
            comparison=self.op.currentData(),
            value=self.value.value(),
            reference=self.reference.currentData(),
        )
        if self.shift.value() or "shift" in d:
            d["shift"] = self.shift.value()
        if d["reference"] == "indicator":
            d.update(
                reference_indicator=self.ref_ind.currentData(),
                reference_period=self.ref_period.value(),
                reference_timeframe=self.ref_tf.currentData(),
                reference_shift=self.ref_shift.value(),
            )
        return d


class GroupEditor(QWidget):
    def __init__(self, data, parent=None, *, indicators=None):
        super().__init__(parent)
        self.indicators = indicators
        self.rows = []
        self.groups = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.op = combo(
            [("Все условия (И)", "AND"), ("Любое условие (ИЛИ)", "OR")],
            data["operator"],
        )
        layout.addWidget(self.op)
        self.body = QVBoxLayout()
        layout.addLayout(self.body)
        for c in data["conditions"]:
            self.add(c)
        for g in data.get("groups", []):
            self.add_group(g)
        layout.addWidget(
            button("+ Добавить фильтр", lambda: self.add(condition()), "secondary")
        )
        layout.addWidget(
            button(
                "+ Группа условий И / ИЛИ",
                lambda: self.add_group(group(condition())),
                "secondary",
            )
        )

    def add(self, data):
        if len(self.rows) >= 32:
            return
        row = ConditionRow(data, indicators=self.indicators)
        row.removed.connect(self.remove)
        self.rows.append(row)
        self.body.addWidget(row)

    def remove(self, row):
        self.rows.remove(row)
        self.body.removeWidget(row)
        row.deleteLater()

    def add_group(self, data):
        if len(self.groups) >= 8:
            return
        container = QFrame()
        container.setObjectName("condition")
        v = QVBoxLayout(container)
        editor = GroupEditor(data, indicators=self.indicators)
        v.addWidget(editor)

        def remove():
            self.groups.remove((container, editor))
            self.body.removeWidget(container)
            container.deleteLater()

        v.addWidget(button("Удалить группу", remove))
        self.groups.append((container, editor))
        self.body.addWidget(container)

    def value_data(self):
        data = {
            "operator": self.op.currentData(),
            "conditions": [r.value_data() for r in self.rows],
        }
        if self.groups:
            data["groups"] = [editor.value_data() for _, editor in self.groups]
        return data


class WeightsEditor(QWidget):
    def __init__(self, rows, key, title, parent=None):
        super().__init__(parent)
        self.key = key
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels([title, "Объём, %"])
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self.table)
        actions = QHBoxLayout()
        actions.addWidget(
            button(
                "+ Ордер",
                lambda: self.add({key: float(self.table.rowCount()), "volume_pct": 10}),
            )
        )
        actions.addWidget(button("Удалить", self.remove))
        layout.addLayout(actions)
        for row in rows:
            self.add(row)
        self.setMinimumHeight(210)

    def add(self, row):
        idx = self.table.rowCount()
        self.table.insertRow(idx)
        self.table.setCellWidget(idx, 0, spin(row[self.key], 0, 10000))
        self.table.setCellWidget(idx, 1, spin(row["volume_pct"], 0.001, 100))

    def remove(self):
        i = self.table.currentRow()
        if i >= 0:
            self.table.removeRow(i)

    def value_data(self):
        return [
            {
                self.key: self.table.cellWidget(i, 0).value(),
                "volume_pct": self.table.cellWidget(i, 1).value(),
            }
            for i in range(self.table.rowCount())
        ]


class Fields(QWidget):
    """Small typed form; choices and bounds are shared by create/edit widgets."""

    def __init__(self, data, specs, parent=None):
        super().__init__(parent)
        self.data = deepcopy(data)
        self.specs = deepcopy(specs)
        self.controls = {}
        form = QFormLayout(self)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setSpacing(14)
        for key, title, kind, options in specs:
            v = data[key]
            if kind == "choice":
                w = combo(options, v)
            elif kind == "bool":
                w = QCheckBox()
                w.setChecked(v)
            elif kind == "optional":
                w = QWidget()
                row = QHBoxLayout(w)
                row.setContentsMargins(0, 0, 0, 0)
                enable = QCheckBox("Включить")
                number = spin(v if v is not None else 0, *options)
                enable.setChecked(v is not None)
                number.setEnabled(v is not None)
                enable.toggled.connect(number.setEnabled)
                row.addWidget(enable)
                row.addWidget(number)
                w.controls = (enable, number)
            elif kind == "text":
                w = QLineEdit(str(v))
            else:
                w = spin(v, *options, integer=kind == "int")
                if kind == "float":
                    # A legacy config may store more than Qt's eight displayed
                    # decimal places. Opening and saving must not rewrite the
                    # number unless this exact field was actually modified.
                    w.setProperty("bccModified", False)
                    w.valueChanged.connect(
                        lambda _value, control=w: control.setProperty(
                            "bccModified", True
                        )
                    )
            w.setObjectName(key)
            form.addRow(title, w)
            self.controls[key] = (kind, w)

    def value_data(self):
        out = deepcopy(self.data)
        for key, (kind, w) in self.controls.items():
            out[key] = (
                w.currentData()
                if kind == "choice"
                else w.isChecked()
                if kind == "bool"
                else (w.controls[1].value() if w.controls[0].isChecked() else None)
                if kind == "optional"
                else w.text()
                if kind == "text"
                else self.data[key]
                if kind == "float" and not w.property("bccModified")
                else w.value()
            )
        return out

    def set_values(self, values):
        for key, value in values.items():
            kind, widget = self.controls[key]
            if kind == "choice":
                widget.setCurrentIndex(widget.findData(value))
            elif kind == "bool":
                widget.setChecked(value)
            elif kind == "text":
                widget.setText(value)
            else:
                widget.setValue(value)


FINANCE = [
    (
        "execution_policy",
        "PAPER-исполнение",
        "choice",
        [("IOC: исполнить доступный объём", "ioc"), ("FOK: только целиком", "fok")],
    ),
    ("leverage", "Плечо", "float", (1, 100)),
    (
        "margin_mode",
        "Тип маржи",
        "choice",
        [("Cross", "CROSS"), ("Isolated", "ISOLATED")],
    ),
    ("allocation_pct", "Доля депозита на секцию, %", "float", (0.001, 100)),
    (
        "reinvest_pct",
        "Реинвест: доля чистой прибыли при включении, %",
        "float",
        (0, 50),
    ),
    ("fee_rate", "Комиссия: доля (0,0006 = 0,06%)", "float", (0, 0.05)),
    ("slippage_pct", "Проскальзывание, %", "float", (0, 5)),
    ("daily_loss_pct", "Максимальный дневной убыток, %", "float", (0.01, 100)),
    ("disabled", "Приостановить новые входы", "bool", None),
    ("stop_after_cycle", "Остановить секцию после сделки", "bool", None),
]
ORDER_FIELDS = [
    (
        "mode",
        "Режим торговли",
        "choice",
        [("Простой", "simple"), ("Свой", "custom"), ("Сигнал", "signal")],
    ),
    ("count", "Количество ордеров всего", "int", (1, 100)),
    ("coverage_pct", "Перекрытие, %", "float", (0.001, 90)),
    ("martingale_pct", "Мартингейл, %", "float", (0, 900)),
    ("first_offset_pct", "Отступ первого ордера, % (0 = рынок)", "float", (0, 90)),
    ("logarithmic", "Логарифмическое распределение", "float", (0.1, 10)),
    ("partial_count", "Активных ордеров сетки одновременно", "int", (1, 100)),
    ("pull_up_pct", "Подтяжка до первого исполнения, %", "float", (0, 100)),
    ("signal_spacing_pct", "Минимальный отступ усреднения, %", "float", (0, 90)),
    (
        "signal_reference",
        "Отсчёт усреднения",
        "choice",
        [("От последнего ордера", "last"), ("От входа", "entry")],
    ),
]
PROFIT_FIELDS = [
    (
        "mode",
        "Тейк-профит",
        "choice",
        [
            ("Простой", "simple"),
            ("Свой: мульти-тейки", "custom"),
            ("Сигнал выхода секции", "signal"),
        ],
    ),
    ("pct", "Простой профит, % от средней цены", "float", (0.001, 10000)),
    (
        "minimum_pct",
        "Минимальный PnL для выхода по сигналу, %",
        "optional",
        (-100, 10000),
    ),
    ("breakeven", "Безубыток после первого мульти-тейка", "bool", None),
    (
        "breakeven_reference",
        "После следующих тейков переносить стоп",
        "choice",
        [
            ("От средней цены", "average"),
            ("От предыдущего тейк-профита", "take_profit"),
        ],
    ),
    ("breakeven_offset_pct", "Отклонение безубытка, %", "float", (-90, 10000)),
    ("trailing", "Трейлинг", "bool", None),
    ("activation_pct", "Активация трейлинга, %", "float", (0.001, 10000)),
    ("distance_pct", "Отклонение трейлинга, %", "float", (0.001, 10000)),
]
STOP_FIELDS = [
    (
        "mode",
        "Стоп-лосс",
        "choice",
        [
            ("Выключен", "none"),
            ("Процент после последнего ордера", "percent"),
            ("По сигналу", "signal"),
            ("Процент и сигнал независимо", "both"),
        ],
    ),
    ("pct", "Отступ за последним уровнем, %", "float", (0.001, 90)),
    (
        "reference",
        "Отсчёт сигнального стопа",
        "choice",
        [("Последний ордер (после всей сетки)", "last"), ("Средняя цена", "average")],
    ),
    ("minimum_pct", "Предельный PnL от точки отсчёта, %", "optional", (-90, 10000)),
    ("stop_after", "Остановить секцию после исполнения стоп-лосса", "bool", None),
]


class SettingsEditor(QTabWidget):
    def __init__(self, data, parent=None, *, strategy="VELES"):
        super().__init__(parent)
        self.data = deepcopy(data)
        finance = FINANCE
        if strategy != "VELES":
            finance = [f for f in finance if f[0] != "reinvest_pct"]
        if strategy == "POLYMARKET":
            finance = [
                f
                for f in FINANCE
                if f[0] in ("daily_loss_pct", "disabled", "stop_after_cycle")
            ]
        elif strategy in ("GRID", "PUMP_DUMP"):
            finance = [f for f in finance if f[0] != "allocation_pct"]
        self.finance = Fields(data, finance)
        self.addTab(scroll(self.finance), "Капитал и риск")
        orders = QWidget()
        layout = QVBoxLayout(orders)
        self.orders = Fields(data["orders"], ORDER_FIELDS)
        presets = QHBoxLayout()
        for title, coverage, count in (
            ("Консервативная · 40% / 20", 40, 20),
            ("Умеренная · 25% / 15", 25, 15),
            ("Агрессивная · 15% / 10", 15, 10),
        ):
            preset = button(
                title,
                lambda checked=False, coverage=coverage, count=count: (
                    self.orders.set_values(
                        {
                            "mode": "simple",
                            "coverage_pct": coverage,
                            "count": count,
                            "martingale_pct": 5.0,
                        }
                    )
                ),
            )
            presets.addWidget(preset)
        layout.addLayout(presets)
        layout.addWidget(self.orders)
        layout.addWidget(
            label("Режим «Свой»: объёмы всех ордеров в сумме 100%", "title")
        )
        self.custom = WeightsEditor(
            data["orders"]["custom"], "offset_pct", "Отступ от входа, %"
        )
        layout.addWidget(self.custom)
        layout.addWidget(label("Режим «Сигнал»: условия усреднения", "title"))
        self.dca = GroupEditor(data["orders"]["signal"])
        layout.addWidget(self.dca)
        self.addTab(scroll(orders), "Ордера и DCA")
        profit = QWidget()
        layout = QVBoxLayout(profit)
        self.profit = Fields(data["profit"], PROFIT_FIELDS)
        layout.addWidget(self.profit)
        self.targets = WeightsEditor(
            data["profit"]["targets"], "pct", "Прибыль от средней цены, %"
        )
        layout.addWidget(self.targets)
        self.addTab(scroll(profit), "Тейк-профит")
        stop = QWidget()
        layout = QVBoxLayout(stop)
        self.stop = Fields(data["stop"], STOP_FIELDS)
        layout.addWidget(self.stop)
        self.stop_signal = GroupEditor(data["stop"]["signal"])
        layout.addWidget(self.stop_signal)
        self.addTab(scroll(stop), "Стоп-лосс")
        if strategy in ("POLYMARKET", "GRID", "PUMP_DUMP"):
            for i in range(1, self.count()):
                self.setTabVisible(i, False)

    def value_data(self):
        data = self.finance.value_data()
        data["orders"] = self.orders.value_data()
        data["orders"]["custom"] = self.custom.value_data()
        data["orders"]["signal"] = self.dca.value_data()
        data["profit"] = self.profit.value_data()
        data["profit"]["targets"] = self.targets.value_data()
        data["stop"] = self.stop.value_data()
        data["stop"]["signal"] = self.stop_signal.value_data()
        return data


class SectionEditor(QWidget):
    def __init__(self, section, settings, parent=None, *, strategy="VELES", label_caption="Название секции", lock_side=False):
        super().__init__(parent)
        self.section = deepcopy(section)
        self.base_settings = deepcopy(settings)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(section["label"])
        self.side = combo([("LONG", "LONG"), ("SHORT", "SHORT")], section["side"])
        self.enabled = QCheckBox()
        self.enabled.setChecked(section["enabled"])
        form.addRow(label_caption, self.name)
        self.side.setEnabled(not lock_side)
        form.addRow("Направление", self.side)
        form.addRow("Включена", self.enabled)
        layout.addLayout(form)
        tabs = QTabWidget()
        self.entry = GroupEditor(section["entry"])
        self.exit = GroupEditor(section["exit"])
        tabs.addTab(scroll(self.entry), "Условия входа")
        tabs.addTab(scroll(self.exit), "Условия выхода")
        over = QWidget()
        v = QVBoxLayout(over)
        self.override = QCheckBox("Индивидуальные ордера, капитал и защита этой секции")
        self.override.setChecked(bool(section.get("overrides")))
        v.addWidget(self.override)
        self.settings = SettingsEditor(
            merge(settings, section.get("overrides", {})), strategy=strategy
        )
        self.settings.setEnabled(self.override.isChecked())
        self.override.toggled.connect(self.settings.setEnabled)
        v.addWidget(self.settings)
        tabs.addTab(over, "Индивидуальные параметры")
        layout.addWidget(tabs)

    def value_data(self):
        s = deepcopy(self.section)
        s.update(
            label=self.name.text(),
            side=self.side.currentData(),
            enabled=self.enabled.isChecked(),
            entry=self.entry.value_data(),
            exit=self.exit.value_data(),
            overrides=difference(self.base_settings, self.settings.value_data())
            if self.override.isChecked()
            else {},
        )
        return s

    def validate_data(self):
        from .config import validate_group, validate_position_settings

        value = self.value_data()
        if not value["label"].strip():
            raise ValueError("Укажите название секции")
        validate_group(value["entry"])
        validate_group(value["exit"])
        validate_position_settings(
            merge(self.base_settings, value["overrides"]), value["side"]
        )


class SectionsEditor(QWidget):
    def __init__(self, sections, settings, parent=None, *, strategy="VELES"):
        super().__init__(parent)
        self.sections = deepcopy(sections)
        self.settings = settings
        self.strategy = strategy
        layout = QVBoxLayout(self)
        layout.addWidget(
            label(
                "Независимые секции: собственные вход, выход и торговые позиции",
                "title",
            )
        )
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["Секция", "Сторона", "Вход", "Выход", "Включена"]
        )
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.doubleClicked.connect(self.edit)
        layout.addWidget(self.table)
        row = QToolBar()
        row.setMovable(False)
        row.setMinimumWidth(0)
        row.addWidget(button("+ Секция", self.add))
        row.addWidget(button("Создать диапазоны RSI", self.generate, "primary"))
        row.addWidget(button("Редактировать", self.edit))
        row.addWidget(button("Удалить", self.remove))
        layout.addWidget(row)
        self.render()

    def render(self):
        selected = self.table.currentRow()
        self.table.setRowCount(len(self.sections))
        for i, s in enumerate(self.sections):

            def desc(g):
                return " + ".join(
                    f"{c['indicator']} {c['timeframe']}m / {'закрытие' if c['trigger'] == 'bar_close' else 'внутри'}"
                    for c in g["conditions"]
                )

            for j, value in enumerate(
                [
                    s["label"],
                    s["side"],
                    desc(s["entry"]),
                    desc(s["exit"]),
                    "Да" if s["enabled"] else "Нет",
                ]
            ):
                self.table.setItem(i, j, QTableWidgetItem(value))
        self.table.resizeRowsToContents()
        if self.sections:
            self.table.selectRow(min(max(selected, 0), len(self.sections) - 1))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "table"):
            QTimer.singleShot(0, self.table.resizeRowsToContents)

    def edit(self, *args):
        i = self.table.currentRow()
        if i < 0:
            return
        settings = self.settings() if callable(self.settings) else self.settings
        editor = SectionEditor(self.sections[i], settings, strategy=self.strategy)
        if confirm_dialog(editor, "Настройки секции", self):
            self.sections[i] = editor.value_data()
            self.render()

    def add(self):
        s = range_section()
        s["id"] = "section_" + uuid.uuid4().hex[:8]
        settings = self.settings() if callable(self.settings) else self.settings
        editor = SectionEditor(s, settings, strategy=self.strategy)
        if confirm_dialog(editor, "Новая секция", self):
            self.sections.append(editor.value_data())
            self.render()
            self.table.selectRow(len(self.sections) - 1)

    def remove(self):
        i = self.table.currentRow()
        if i >= 0:
            del self.sections[i]
            self.render()

    def generate(self):
        w = QWidget()
        form = QFormLayout(w)
        start = spin(20, 0, 99)
        finish = spin(80, 1, 100)
        step = spin(10, 0.1, 100)
        side = combo([("LONG", "LONG"), ("SHORT", "SHORT")])
        entry = combo([(f"{m} мин", m) for m in TIMEFRAMES], 2)
        exit_ = combo([(f"{m} мин", m) for m in TIMEFRAMES], 1)
        for title, c in [
            ("Начало", start),
            ("Конец", finish),
            ("Шаг диапазона", step),
            ("Направление", side),
            ("Таймфрейм входа", entry),
            ("Таймфрейм выхода", exit_),
        ]:
            form.addRow(title, c)
        if confirm_dialog(w, "Добавить цепочку RSI", self):
            a = start.value()
            b = finish.value()
            delta = step.value()
            if a >= b or (b - a) / delta > 100:
                QMessageBox.warning(
                    self, "Диапазоны", "Проверьте начало, конец и количество диапазонов"
                )
                return
            while a < b - 1e-8:
                end = min(b, a + delta)
                s = (
                    range_section(
                        a, end, "LONG", entry.currentData(), exit_.currentData()
                    )
                    if side.currentData() == "LONG"
                    else range_section(
                        end, a, "SHORT", entry.currentData(), exit_.currentData()
                    )
                )
                if s["id"] not in {x["id"] for x in self.sections}:
                    self.sections.append(s)
                a = end
            self.render()

    def value_data(self):
        return deepcopy(self.sections)


class _VelesRangeTree(QTreeWidget):
    """True two-level tree; selectRow supports legacy Qt acceptance helpers."""

    def selectRow(self, row):
        leaves = [self.topLevelItem(i).child(j)
                  for i in range(self.topLevelItemCount())
                  for j in range(self.topLevelItem(i).childCount())]
        if 0 <= row < len(leaves):
            self.setCurrentItem(leaves[row])


class VelesSectionsEditor(QWidget):
    """Секция → диапазоны RSI, serialized to established flat worker sections.

    No synthetic 'step' is created. Parent rows group and batch-edit ranges;
    child rows remain the same independent execution identities in SQLite.
    """

    def __init__(self, sections, settings, parent=None, *, strategy="VELES"):
        super().__init__(parent)
        from .section_hierarchy import grouped_ranges
        self.sections = deepcopy(sections)
        self.settings = settings
        self.strategy = strategy
        layout = QVBoxLayout(self)
        layout.addWidget(label("Секции и вложенные диапазоны RSI", "title"))
        layout.addWidget(label(
            "Секция объединяет диапазоны одного направления. Каждый диапазон "
            "сохраняет свои сигналы входа/выхода и историю PAPER.", "muted"))
        self.table = _VelesRangeTree()
        self.table.setObjectName("velesSectionRangeTree")
        self.table.setColumnCount(4)
        self.table.setHeaderLabels(["Секция / диапазон", "Направление", "Вход", "Выход"])
        self.table.header().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.header().setStretchLastSection(True)
        self.table.itemDoubleClicked.connect(lambda *_: self.edit())
        layout.addWidget(self.table, 1)
        bar = QHBoxLayout()
        bar.addWidget(button("+ Секция", self.add))
        bar.addWidget(button("+ Диапазон RSI", self.add_range, "primary"))
        bar.addWidget(button("Цепочка диапазонов", self.generate))
        bar.addWidget(button("Редактировать", self.edit))
        bar.addWidget(button("Удалить", self.remove))
        layout.addLayout(bar)
        self.render()

    def _groups(self):
        from .section_hierarchy import grouped_ranges
        return grouped_ranges(self.sections)

    def _checked_sections(self, proposed):
        """Stage the complete edit through the shared VELES rule contract.

        A standalone widget without a parent wizard can still be inspected;
        the actual wizard installs its current-form provider after setup.
        """
        provider = getattr(self, "config_provider", None)
        if provider is None:
            return deepcopy(proposed)
        from .constructor_sections import project_constructor_sections_transaction
        return project_constructor_sections_transaction(
            provider(), sections=proposed)["sections"]

    def render(self, select=None):
        previous = self.table.currentItem()
        if select is None and previous is not None:
            select = previous.data(0, Qt.ItemDataRole.UserRole)
        self.table.clear()
        for parent in self._groups():
            children = parent["sections"]
            g = QTreeWidgetItem([
                f"Секция · {parent['name']} ({len(children)} диапаз.)",
                parent["side"], "", "",
            ])
            g.setData(0, Qt.ItemDataRole.UserRole, ("group", parent["id"]))
            self.table.addTopLevelItem(g)
            for s in children:
                from .section_hierarchy import rsi_bounds
                bounds = rsi_bounds(s)
                if bounds is None:
                    description = f"Диапазон · {s['label']}"
                else:
                    description = f"Диапазон · RSI {bounds[0]:g} → {bounds[1]:g}"
                entry = ", ".join(str(c.get("timeframe", "?")) + "м" for c in s["entry"]["conditions"])
                exit_ = ", ".join(str(c.get("timeframe", "?")) + "м" for c in s["exit"]["conditions"])
                child = QTreeWidgetItem([description, s["side"], entry, exit_])
                child.setData(0, Qt.ItemDataRole.UserRole, ("range", s["id"]))
                if not s["enabled"]:
                    child.setText(1, s["side"] + " · выкл")
                g.addChild(child)
                if select == ("range", s["id"]):
                    self.table.setCurrentItem(child)
            if select == ("group", parent["id"]):
                self.table.setCurrentItem(g)
            g.setExpanded(True)
        if self.table.currentItem() is None and self.table.topLevelItemCount():
            root = self.table.topLevelItem(0)
            self.table.setCurrentItem(root.child(0) if root.childCount() else root)

    def _selection(self):
        item = self.table.currentItem()
        if item is None:
            return None
        value = item.data(0, Qt.ItemDataRole.UserRole)
        return tuple(value) if isinstance(value, (tuple, list)) and len(value) == 2 else None

    def _selected_group(self):
        selected = self._selection()
        groups = self._groups()
        if not selected:
            return groups[0] if groups else None
        for g in groups:
            if selected == ("group", g["id"]) or any(
                selected == ("range", s["id"]) for s in g["sections"]
            ):
                return g
        return None

    def _confirm_mutation(self, widget, title, staged, validator):
        """Commit only a validated, complete hierarchy transaction.

        The native dialog runs its validator before accepting.  External
        dialog adapters may return Accepted without invoking the callback;
        such a response must never publish an empty or partially validated
        hierarchy.  Revalidate exactly once if no staged result exists.
        """
        if not confirm_dialog(widget, title, self, validator=validator):
            return False
        if not staged:
            validator()
        if 'sections' not in staged:
            raise ValueError('Нет проверенного результата изменения секций')
        return True

    def edit(self, *unused):
        selected = self._selection()
        if not selected:
            return
        if selected[0] == "range":
            i = next((i for i, s in enumerate(self.sections) if s["id"] == selected[1]), None)
            if i is None:
                return
            settings = self.settings() if callable(self.settings) else self.settings
            editor = SectionEditor(self.sections[i], settings, strategy=self.strategy,
                                   label_caption="Название диапазона", lock_side=True)
            from .section_hierarchy import replace_range
            accepted = {}
            def check_range():
                accepted["sections"] = self._checked_sections(
                    replace_range(self.sections, selected[1], editor.value_data()))
            if self._confirm_mutation(editor, "Настройки диапазона RSI",
                                      accepted, check_range):
                self.sections = accepted["sections"]
                self.render(select=selected)
            return
        group = self._selected_group()
        if group is None:
            return
        w = QWidget()
        form = QFormLayout(w)
        name = QLineEdit(group["name"])
        enabled = QCheckBox("Включить все диапазоны секции")
        # Tri-state reflects a real mixed section, but opening the dialog or
        # renaming a group must NEVER rewrite every range's enabled flag.
        states = {s["enabled"] for s in group["sections"]}
        enabled.setTristate(len(states) > 1)
        if len(states) > 1:
            enabled.setCheckState(Qt.CheckState.PartiallyChecked)
        else:
            enabled.setChecked(True in states)
        enabled_touched = {"value": False}
        enabled.stateChanged.connect(lambda *_: enabled_touched.__setitem__("value", True))
        form.addRow("Название секции", name)
        form.addRow("Направление", label(group["side"] + " (фиксировано для диапазонов)"))
        form.addRow("Состояние", enabled)
        form.addRow(label("RSI-уровни и составные сигналы редактируются внутри диапазонов.", "muted"))
        apply_settings = QCheckBox("Применить единые ордера, DCA и защиту ко всем диапазонам")
        form.addRow(apply_settings)
        base_settings = self.settings() if callable(self.settings) else self.settings
        common = SettingsEditor(merge(base_settings, group["sections"][0].get("overrides", {})),
                                strategy=self.strategy)
        common.setEnabled(False)
        apply_settings.toggled.connect(common.setEnabled)
        outer = w.layout()
        outer.addRow(common)
        accepted = {}
        def validate_group_edit():
            if not 1 <= len(name.text().strip()) <= 120:
                raise ValueError("Название секции: от 1 до 120 символов")
            from .section_hierarchy import adopt_group, grouped_ranges
            draft = deepcopy(self.sections)
            trial = next(g for g in grouped_ranges(draft) if g["id"] == group["id"])
            gid = adopt_group(draft, trial, name.text())
            inherited = (difference(base_settings, common.value_data())
                         if apply_settings.isChecked() else None)
            for section in trial["sections"]:
                if (enabled_touched["value"]
                        and enabled.checkState() != Qt.CheckState.PartiallyChecked):
                    section["enabled"] = enabled.checkState() == Qt.CheckState.Checked
                if inherited is not None:
                    section["overrides"] = deepcopy(inherited)
            accepted["sections"] = self._checked_sections(draft)
            accepted["gid"] = gid
        if self._confirm_mutation(w, "Настройки секции", accepted,
                                  validate_group_edit):
            self.sections = accepted["sections"]
            self.render(select=("group", accepted["gid"]))

    def add(self):
        w = QWidget()
        form = QFormLayout(w)
        name = QLineEdit("Новая секция RSI")
        side = combo([("LONG", "LONG"), ("SHORT", "SHORT")])
        start = spin(30, 0, 100)
        finish = spin(40, 0, 100)
        entry = combo([(f"{m} мин", m) for m in TIMEFRAMES], 2)
        exit_ = combo([(f"{m} мин", m) for m in TIMEFRAMES], 1)
        for text, field in [("Название секции", name), ("Направление", side),
                            ("Начало RSI", start), ("Конец RSI", finish),
                            ("Вход", entry), ("Выход", exit_)]:
            form.addRow(text, field)
        accepted = {}
        def check():
            if not name.text().strip():
                raise ValueError("Укажите название секции")
            if (side.currentData() == "LONG" and start.value() >= finish.value()) or (
                    side.currentData() == "SHORT" and start.value() <= finish.value()):
                raise ValueError("Проверьте направление RSI-диапазона")
            if len(self.sections) >= 100:
                raise ValueError("Не более 100 торговых диапазонов")
            from .section_hierarchy import PARENT_ID, PARENT_LABEL
            section = range_section(start.value(), finish.value(), side.currentData(),
                                    entry.currentData(), exit_.currentData())
            section["id"] = "range_" + uuid.uuid4().hex[:12]
            section[PARENT_ID] = "parent_" + uuid.uuid4().hex[:12]
            section[PARENT_LABEL] = name.text().strip()
            accepted["sections"] = self._checked_sections(self.sections + [section])
            accepted["id"] = section["id"]
        if self._confirm_mutation(w, "Новая секция с первым диапазоном", accepted, check):
            self.sections = accepted["sections"]
            self.render(select=("range", accepted["id"]))

    def add_range(self):
        group = self._selected_group()
        if group is None:
            return self.add()
        from .section_hierarchy import (rsi_bounds, adopt_group,
                                        append_rsi_range, grouped_ranges)
        last = rsi_bounds(group["sections"][-1])
        start_value = last[1] if last else (30 if group["side"] == "LONG" else 70)
        end_value = max(0, min(100, start_value + (10 if group["side"] == "LONG" else -10)))
        w = QWidget()
        form = QFormLayout(w)
        start = spin(start_value, 0, 100)
        finish = spin(end_value, 0, 100)
        entry = combo([(f"{m} мин", m) for m in TIMEFRAMES], last[2] if last else 2)
        exit_ = combo([(f"{m} мин", m) for m in TIMEFRAMES], last[3] if last else 1)
        for title, control in [("Начало RSI", start), ("Конец RSI", finish),
                               ("Таймфрейм входа", entry), ("Таймфрейм выхода", exit_)]:
            form.addRow(title, control)
        accepted = {}
        def check():
            if len(self.sections) >= 100:
                raise ValueError("Не более 100 торговых диапазонов")
            if (group["side"] == "LONG" and start.value() >= finish.value()) or (
                    group["side"] == "SHORT" and start.value() <= finish.value()):
                raise ValueError("Проверьте направление RSI-диапазона")
            # Validate the *whole* staged hierarchy before the native dialog
            # accepts. On an inherited-risk error, keep all fields editable.
            draft = deepcopy(self.sections)
            trial = next(g for g in grouped_ranges(draft) if g["id"] == group["id"])
            gid = adopt_group(draft, trial)
            added = append_rsi_range(draft, gid, group["name"], start.value(),
                                     finish.value(), entry.currentData(), exit_.currentData())
            accepted["sections"] = self._checked_sections(draft)
            accepted["id"] = added["id"]
        if self._confirm_mutation(w, "Диапазон внутри секции", accepted, check):
            self.sections = accepted["sections"]
            self.render(select=("range", accepted["id"]))

    def remove(self):
        selected = self._selection()
        if not selected:
            return
        if selected[0] == "group":
            group = self._selected_group()
            if group is None:
                return
            if QMessageBox.question(self, "Удалить секцию",
                    f"Удалить секцию «{group['name']}» и все её {len(group['sections'])} диапазонов?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
            if len(self.sections) <= len(group["sections"]):
                QMessageBox.warning(self, "Секция", "В боте должна остаться хотя бы одна секция и один диапазон.")
                return
            ids = {s["id"] for s in group["sections"]}
            proposed = [s for s in self.sections if s["id"] not in ids]
        else:
            if len(self.sections) <= 1:
                QMessageBox.warning(self, "Диапазон", "В боте должен остаться хотя бы один диапазон.")
                return
            if QMessageBox.question(
                    self, "Удалить диапазон",
                    "Удалить диапазон из конфигурации? Сохранённая история PAPER при этом не удаляется.",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
            proposed = [s for s in self.sections if s["id"] != selected[1]]
        try:
            checked = self._checked_sections(proposed)
        except ValueError as exc:
            QMessageBox.warning(self, "Диапазоны", str(exc))
            return
        self.sections = checked
        self.render()

    def generate(self):
        """Generate a checked RSI chain in the chosen group, without steps."""
        group = self._selected_group()
        if group is None:
            return self.add()
        from .section_hierarchy import (adopt_group, append_rsi_range,
                                        rsi_bounds, grouped_ranges)
        w = QWidget()
        form = QFormLayout(w)
        start = spin(30 if group["side"] == "LONG" else 70, 0, 100)
        finish = spin(70 if group["side"] == "LONG" else 30, 0, 100)
        step = spin(10, 0.1, 100)
        entry = combo([(f"{m} мин", m) for m in TIMEFRAMES], 2)
        exit_ = combo([(f"{m} мин", m) for m in TIMEFRAMES], 1)
        for title, control in [("Начало", start), ("Конец", finish),
                               ("Шаг RSI", step), ("Таймфрейм входа", entry),
                               ("Таймфрейм выхода", exit_)]:
            form.addRow(title, control)
        accepted = {}
        def check():
            direction = 1 if group["side"] == "LONG" else -1
            count = direction * (finish.value() - start.value()) / step.value()
            if count <= 0 or count > 100 or len(self.sections) + int(count + .9999) > 100:
                raise ValueError("Недопустимое начало, конец или число диапазонов")
            existing = {rsi_bounds(s)[:2] for s in group["sections"] if rsi_bounds(s)}
            draft = deepcopy(self.sections)
            trial = next(g for g in grouped_ranges(draft) if g["id"] == group["id"])
            gid = adopt_group(draft, trial)
            a = start.value()
            added = None
            while direction * (finish.value() - a) > 1e-8:
                b = a + direction * min(step.value(), abs(finish.value() - a))
                if (float(a), float(b)) in existing:
                    raise ValueError(f"Диапазон RSI {a:g} → {b:g} уже существует")
                added = append_rsi_range(draft, gid, group["name"], a, b,
                                         entry.currentData(), exit_.currentData())
                a = b
            accepted["sections"] = self._checked_sections(draft)
            accepted["id"] = added["id"] if added else ""
        if confirm_dialog(w, "Цепочка диапазонов внутри секции", self,
                          validator=check):
            self.sections = accepted["sections"]
            self.render(select=("range", accepted["id"]))

    def value_data(self):
        return deepcopy(self.sections)
