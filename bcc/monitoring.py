"""Read-only modeless monitoring across all strategy folders."""

import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
)

from .market_health import display_market_status
from .config import STRATEGY_NAMES
from .liquidation import status as liquidation_status
from .market_clock import quote_is_recent
from .position_report import position_mark_pnl
from .storage import Store
from .trading import available_margin, equity, position_unrealized
from .widgets import button, dialog_size, label

ACTIVE = {"Работает", "Запуск", "Остановка", "Завершает сделки"}


def snapshot_all(repo, life, include_stopped=False, include_history=False):
    records = []
    for folder, cfg in repo.list():
        row = dict(path=str(folder), config=cfg, state=None, process=None, error="", history=[])
        try:
            row["status"] = life.status(folder)
            row["process"] = life.active_snapshot(folder)
            if cfg.get("invalid"):
                raise ValueError(cfg["invalid"])
            if (folder / "paper.sqlite").exists():
                row["state"] = Store(folder).read(include_charts=False)
                if include_history:
                    row["history"] = Store(folder).history(100)
            if row["status"] == "Остановлен":
                row["error"] = life.last_error(folder)
                if row["error"]:
                    row["status"] = "Остановлен с ошибкой"
                state = row["state"] or {}
                if (
                    not include_stopped
                    and not row["error"]
                    and not state.get("positions")
                    and not state.get("pending")
                ):
                    continue
        except Exception as exc:
            row["status"] = "Ошибка"
            row["error"] = str(exc)
        records.append(row)
    return records


def position_pnl(state, position, now):
    return position_mark_pnl(state, position, now)


def number(value, signed=False):
    return "—" if value is None else format(value, "+.4f" if signed else ".4f")


class _SnapshotSignals(QObject):
    finished = Signal(object)


class _SnapshotTask(QRunnable):
    """Keep disk/SQLite/process inspection off the GUI event loop.

    The global thread pool retains the task while it is running, so closing
    the window never destroys a live QThread. No QWidget is accessed here.
    """

    def __init__(self, repo, life, include_stopped, include_history, signals, generation):
        super().__init__()
        self.repo = repo
        self.life = life
        self.include_stopped = include_stopped
        self.include_history = include_history
        self.signals = signals
        self.generation = generation

    def run(self):
        try:
            records = snapshot_all(self.repo, self.life, self.include_stopped,
                                   self.include_history)
            self.signals.finished.emit((self.generation, records, None))
        except Exception as exc:
            self.signals.finished.emit((self.generation, None, str(exc)))


class RunningBotsWindow(QDialog):
    open_bot = Signal(str)

    def __init__(self, repo, life, parent=None, refresh_ms=2000):
        super().__init__(parent)
        from .ui import table

        self.repo, self.life = repo, life
        self.records = []
        self._raw_records = []
        self._loading = False
        self._refresh_pending = False
        self._closed = False
        self._generation = 0
        self._requested_snapshot = 0
        self.completed_snapshot = 0
        self._active_snapshot = 0
        self._snapshot_signals = _SnapshotSignals()
        self._snapshot_signals.finished.connect(self._snapshot_received)
        self._snapshot_pool = QThreadPool(self)
        self._snapshot_pool.setMaxThreadCount(1)
        self.setObjectName("runningBotsMonitor")
        self.setWindowTitle("Монитор всех ботов · PAPER")
        self.setModal(False)
        dialog_size(self, 1500, 950)
        layout = QVBoxLayout(self)
        layout.addWidget(label("Работающие боты и открытые позиции", "headline"))
        layout.addWidget(
            label(
                "Счета PAPER независимы. Просмотр не меняет настройки и торговлю.",
                "muted",
            )
        )
        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Бот, стратегия или монета")
        filters.addWidget(self.search, 1)
        self.include_stopped = QCheckBox("Также остановленные без позиций")
        filters.addWidget(self.include_stopped)
        layout.addLayout(filters)
        self.summary = label("", "title")
        layout.addWidget(self.summary)
        self.alert = label("", "title")
        self.alert.setWordWrap(True)
        self.alert.hide()
        layout.addWidget(self.alert)
        self.bots = table(
            [
                "Бот",
                "Стратегия",
                "Монеты",
                "Процесс",
                "Heartbeat, с",
                "Баланс",
                "Капитал",
                "Своб. маржа",
                "Позиции",
                "Ожид. сетки",
                "Realized PnL",
                "Unrealized PnL",
                "Макс. DD, %",
                "Сигналы / ошибки",
            ]
        )
        layout.addWidget(self.bots, 1)
        self.tabs = QTabWidget()
        self.positions = table(
            [
                "Бот",
                "Стратегия",
                "Монета",
                "Секция",
                "Сторона",
                "Количество",
                "Средняя",
                "Unrealized PnL",
                "DCA сейчас",
                "Состояние",
                "Открыта (местн.)",
            ]
        )
        self.signals = table(
            ["Бот", "Монета", "Секция", "Индикатор / TF", "Значения", "Причина"]
        )
        self.history = table(
            [
                "Время (местн.)",
                "Бот",
                "Монета",
                "Секция",
                "Действие",
                "Net PnL",
                "Причина",
            ]
        )
        for widget, title in [
            (self.positions, "Все позиции"),
            (self.signals, "Все сигналы"),
            (self.history, "Последние исполнения"),
        ]:
            self.tabs.addTab(widget, title)
        layout.addWidget(self.tabs, 1)
        for widget in [self.bots, self.positions, self.signals, self.history]:
            widget.horizontalHeader().setSectionResizeMode(
                QHeaderView.ResizeMode.ResizeToContents
            )
            widget.horizontalHeader().setStretchLastSection(True)
        actions = QToolBar()
        actions.setMovable(False)
        actions.addWidget(
            button("Открыть выбранного бота", self.open_selected, "primary")
        )
        actions.addWidget(button("Обновить", self.refresh))
        actions.addWidget(button("Закрыть монитор", self.close))
        layout.addWidget(actions)
        self.updated = label("", "muted")
        layout.addWidget(self.updated)
        self.bots.doubleClicked.connect(self.open_selected)
        self.search.textChanged.connect(self.refresh)
        self.include_stopped.toggled.connect(self.refresh)
        self.tabs.currentChanged.connect(self.refresh)
        self.timer = QTimer(self)
        self.timer.setInterval(refresh_ms)
        self.timer.timeout.connect(self._timer_refresh)

    def showEvent(self, event):
        self._closed = False
        self.refresh()
        self.timer.start()
        super().showEvent(event)

    def closeEvent(self, event):
        self._closed = True
        self._generation += 1  # Invalidate in-flight results after closing.
        self._refresh_pending = False
        self.timer.stop()
        super().closeEvent(event)

    def open_selected(self, *args):
        item = self.bots.item(self.bots.currentRow(), 0)
        if item is not None:
            self.open_bot.emit(item.data(Qt.ItemDataRole.UserRole))

    def _timer_refresh(self):
        if not self._loading and not self._closed:
            self.refresh()

    def refresh(self, *args):
        self._requested_snapshot += 1
        request_id = self._requested_snapshot
        # Never queue an unbounded number of snapshots under heavy load.
        # Filters can immediately re-render the last complete snapshot.
        if self._raw_records:
            self._render(self._raw_records)
        if self._loading:
            self._refresh_pending = True
            return request_id
        self._loading = True
        self._active_snapshot = request_id
        task = _SnapshotTask(
            self.repo, self.life, self.include_stopped.isChecked(),
            self.tabs.currentWidget() is self.history,
            self._snapshot_signals, self._generation,
        )
        self._snapshot_pool.start(task)
        return request_id

    @Slot(object)
    def _snapshot_received(self, result):
        self._loading = False
        if self._closed:
            return
        generation, records, error = result
        if generation != self._generation:
            self._refresh_pending = False
            if not self._closed:
                self.refresh()
            return
        if error is not None:
            # A whole-fleet failure must invalidate the previous snapshot.
            # In particular, never continue displaying a cached RUNNING bot
            # after inventory/SQLite inspection failed before any rows arrived.
            self._raw_records = []
            self._render([])
            self.alert.setText("Ошибка обновления монитора: " + error)
            self.alert.show()
        else:
            self.alert.hide()
            self._raw_records = records
            self._render(records)
        self.completed_snapshot = self._active_snapshot
        if self._refresh_pending:
            self._refresh_pending = False
            self.refresh()

    def _render(self, records):
        from .ui import populate

        now = time.time()
        query = self.search.text().casefold().strip()
        self.records = [
            r
            for r in records
            if not query
            or query
            in (
                r["config"].get("name", "")
                + " "
                + r["config"].get("strategy", "")
                + " "
                + " ".join(r["config"].get("symbols", []))
            ).casefold()
        ]
        rows, keys, positions, position_keys, signals, executions = (
            [],
            [],
            [],
            [],
            [],
            [],
        )
        active = sum(r["status"] in ACTIVE for r in records)
        open_count = sum(len(r["state"]["positions"]) for r in records if r["state"])
        errors = 0
        issues = []
        for record in self.records:
            cfg, state = record["config"], record["state"]
            name, strategy = cfg.get("name", "?"), cfg.get("strategy", "?")
            process = record["process"] or {}
            age = (
                max(0, now - process["heartbeat"]) if process.get("heartbeat") else None
            )
            value, unknown = equity(state) if state else (None, True)
            upnl = None
            if state and not unknown:
                upnl = sum(
                    position_pnl(state, p, now) or 0
                    for p in state["positions"].values()
                )
            reasons = [
                d.get("reason", "")
                for ds in (state or {}).get("diagnostics", {}).values()
                for d in ds
            ]
            issue = record["error"] or (
                "Нет свежего heartbeat" if age is not None and age > 15 else ""
            )
            errors += bool(issue)
            if issue:
                issues.append(name + ": " + issue)
            rows.append(
                [
                    name,
                    STRATEGY_NAMES.get(strategy, strategy),
                    ", ".join(cfg.get("symbols", [])[:3])
                    + (
                        f" · ещё {len(cfg['symbols']) - 3}"
                        if len(cfg.get("symbols", [])) > 3
                        else ""
                    ),
                    display_market_status(record["status"], process, now=now),
                    number(age),
                    number(state["cash"] if state else None),
                    number(None if unknown else value),
                    number(available_margin(state) if state and not unknown else None),
                    len(state["positions"]) if state else 0,
                    len(state["pending"]) if state else 0,
                    number(state.get("realized", 0) if state else None, True),
                    number(upnl, True),
                    number(state.get("max_drawdown", 0) if state else None),
                    issue or " · ".join(dict.fromkeys(reasons)),
                ]
            )
            keys.append(record["path"])
            for key, p in (state or {}).get("positions", {}).items():
                positions.append(
                    [
                        name,
                        STRATEGY_NAMES.get(strategy, strategy),
                        p["symbol"],
                        p["section"],
                        p["side"],
                        f"{p['qty']:.8g}",
                        f"{p['average']:.8g}",
                        number(position_pnl(state, p, now), True),
                        p.get("dca_fills", 0),
                        (
                            "Сопровождение приостановлено"
                            + (
                                " · " + liquidation_status(p)
                                if liquidation_status(p)
                                else ""
                            )
                            if record["status"] not in ACTIVE
                            else liquidation_status(p)
                            or p.get("exit_pending", {}).get("reason", "Открыта")
                        ),
                        datetime.fromtimestamp(p["opened"])
                        .astimezone()
                        .strftime("%Y-%m-%d %H:%M:%S"),
                    ]
                )
                position_keys.append(record["path"] + ":" + key)
            for sym, ds in (state or {}).get("diagnostics", {}).items():
                for d in ds:
                    conditions = d.get("conditions") or [None]
                    for c in conditions:
                        values = (
                            f"{c['value']:.6g} {c.get('comparison', '')} {c.get('reference', 0):.6g}"
                            if c and c.get("available")
                            else "—"
                        )
                        signals.append(
                            [
                                name,
                                sym,
                                d.get("section", ""),
                                f"{c['indicator']} / {c['timeframe']}m" if c else "—",
                                values,
                                d.get("reason", ""),
                            ]
                        )
            if state and self.tabs.currentWidget() is self.history:
                executions.extend((e, name) for e in record.get("history", []))
        populate(self.bots, rows, keys)
        self.alert.setText("Причины ошибок: " + " · ".join(issues[:3]))
        self.alert.setVisible(bool(issues))
        symbols_by_path = {
            r["path"]: r["config"].get("symbols", []) for r in self.records
        }
        for i in range(self.bots.rowCount()):
            path = self.bots.item(i, 0).data(Qt.ItemDataRole.UserRole)
            self.bots.item(i, 2).setToolTip(", ".join(symbols_by_path.get(path, [])))
        populate(self.positions, positions, position_keys)
        populate(self.signals, signals)
        executions.sort(key=lambda row: row[0]["at"], reverse=True)
        populate(
            self.history,
            [
                [
                    datetime.fromtimestamp(e["at"])
                    .astimezone()
                    .strftime("%Y-%m-%d %H:%M:%S"),
                    name,
                    e["symbol"],
                    e["section"],
                    e["action"],
                    number(e.get("net_pnl", 0), True),
                    e.get("reason", ""),
                ]
                for e, name in executions[:1000]
            ],
            [e["id"] for e, _ in executions[:1000]],
        )
        self.summary.setText(
            f"Работающих: {active} · Позиций: {open_count} · Показано ботов: {len(rows)} · Ошибок / heartbeat: {errors}"
        )
        self.updated.setText(
            "Обновлено: "
            + datetime.now().astimezone().strftime("%H:%M:%S")
            + " · «—» означает отсутствие свежей оценки"
        )
