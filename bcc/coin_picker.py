"""Searchable multi-coin selection, with truthful market availability semantics.

Polymarket selection uses the supported strategy catalog; it MUST NOT depend on
which unrelated Gamma markets happen to appear on the first 2,000 API rows.
Availability is verified separately by PolymarketData.discover(coin, duration)
when the PAPER worker selects a concrete market.
"""

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QVBoxLayout,
)

from .widgets import button, dialog_size, label


class InventorySignals(QObject):
    loaded = Signal(list, str)


class InventoryJob(QRunnable):
    def __init__(self, strategy):
        super().__init__()
        self.strategy = strategy
        self.signals = InventorySignals()

    def run(self):
        from .market import PolymarketData, PublicData

        source = None
        try:
            source = PolymarketData() if self.strategy == "POLYMARKET" else PublicData()
            symbols = source.list_symbols()
            if not symbols:
                raise ValueError("Источник вернул пустой список торговых пар")
            self.signals.loaded.emit(symbols, "")
        except Exception as exc:
            # Initialization failures must also reach the UI. Otherwise the
            # button remains disabled forever with a misleading spinner.
            self.signals.loaded.emit([], str(exc))
        finally:
            if source is not None:
                source.close()


class CoinPicker(QDialog):
    def __init__(self, strategy, symbols, parent=None):
        super().__init__(parent)
        self.strategy = strategy
        self.result_symbols = list(symbols)
        self._inventory_loaded = False
        self._inventory_requested = False
        self._loading = False
        self._job = None
        self.setWindowTitle("Выбор монет")
        dialog_size(self, 1000, 700)
        layout = QVBoxLayout(self)
        self.notice = label(
            "Монеты для выбора загружаются автоматически. Ранее добавленные монеты сохраняются.",
            "muted",
        )
        layout.addWidget(self.notice)
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск монеты")
        self.search.textChanged.connect(self.filter)
        top.addWidget(self.search)
        self.load_button = button("Обновить список", self.load, "primary")
        top.addWidget(self.load_button)
        layout.addLayout(top)
        columns = QHBoxLayout()
        left = QVBoxLayout()
        right = QVBoxLayout()
        columns.addLayout(left, 1)
        columns.addLayout(right, 1)
        layout.addLayout(columns, 1)
        left.addWidget(label(
            "Поддерживаемые монеты" if strategy == "POLYMARKET" else "Доступные на Bybit",
            "title",
        ))
        right.addWidget(label("Добавлены в бота", "title"))
        self.available = QListWidget()
        self.chosen = QListWidget()
        for view in (self.available, self.chosen):
            view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        left.addWidget(self.available)
        right.addWidget(self.chosen)
        self.chosen.addItems(list(dict.fromkeys(symbols)))
        select = QHBoxLayout()
        select.addWidget(button("Выбрать все", self.select_visible))
        select.addWidget(button("Снять все", self.available.clearSelection))
        left.addLayout(select)
        left.addWidget(button("Добавить выбранные", self.add_selected))
        left.addWidget(button(
            "Добавить все поддерживаемые" if strategy == "POLYMARKET"
            else "Добавить все доступные", self.add_all,
        ))
        right.addWidget(button("Удалить выбранные", self.remove_selected))
        right.addWidget(button("Удалить все", self.clear_chosen))
        self.count = label("")
        layout.addWidget(self.count)
        self.update_count()
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.commit)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if strategy == "POLYMARKET":
            # This is a supported catalog, not a live market inventory. Rendering
            # it synchronously prevents an empty picker due to Gamma API delays.
            from .market import COIN_NAMES

            self.loaded(sorted(COIN_NAMES), "")
        # Other strategies use Bybit's live instrument inventory. It starts
        # after the dialog becomes visible, so users see progress or errors.

    def showEvent(self, event):
        super().showEvent(event)
        if not self._inventory_loaded and not self._inventory_requested:
            QTimer.singleShot(0, self._auto_load)

    def _auto_load(self):
        if self.isVisible() and not self._inventory_loaded and not self._inventory_requested:
            self.load()

    def load(self):
        if self._loading:
            return
        self._inventory_requested = True
        if self.strategy == "POLYMARKET":
            # No HTTP call is needed for the selection catalog. Market validity
            # must be checked separately for each duration at trade admission.
            from .market import COIN_NAMES

            self.loaded(sorted(COIN_NAMES), "")
            return
        self._loading = True
        self.load_button.setEnabled(False)
        self.notice.setText("Загружаются торговые пары Bybit…")
        self._job = InventoryJob(self.strategy)
        self._job.signals.loaded.connect(self.loaded)
        QThreadPool.globalInstance().start(self._job)

    def loaded(self, symbols, error):
        self._loading = False
        self.load_button.setEnabled(True)
        if error or not symbols:
            self.notice.setText(
                "Не удалось получить список с биржи: "
                + (error or "Пустой ответ API")
                + ". Повторите загрузку. Ранее выбранные монеты сохранены."
            )
            return
        self._inventory_loaded = True
        # Replacing the available list NEVER modifies the chosen symbols.
        self.available.clear()
        self.available.addItems(sorted(set(symbols)))
        if self.strategy == "POLYMARKET":
            self.notice.setText(
                f"Поддерживаются {len(set(symbols))} монет. "
                "Наличие активного UP/DOWN-рынка проверяется отдельно для "
                "каждой длительности перед PAPER-входом."
            )
        else:
            self.notice.setText(f"Получено бессрочных USDT-пар Bybit: {len(set(symbols))}")
        self.filter()

    def filter(self, *args):
        needle = self.search.text().upper().strip()
        for i in range(self.available.count()):
            self.available.item(i).setHidden(
                needle not in self.available.item(i).text().upper()
            )

    def select_visible(self):
        self.available.clearSelection()
        for i in range(self.available.count()):
            self.available.item(i).setSelected(not self.available.item(i).isHidden())

    def add(self, symbols):
        existing = {self.chosen.item(i).text() for i in range(self.chosen.count())}
        for symbol in symbols:
            if symbol not in existing:
                self.chosen.addItem(symbol)
                existing.add(symbol)
        self.update_count()

    def add_selected(self):
        self.add([i.text() for i in self.available.selectedItems() if not i.isHidden()])

    def add_all(self):
        self.add([self.available.item(i).text() for i in range(self.available.count())])

    def remove_selected(self):
        for item in self.chosen.selectedItems():
            self.chosen.takeItem(self.chosen.row(item))
        self.update_count()

    def clear_chosen(self):
        self.chosen.clear()
        self.update_count()

    def update_count(self):
        self.count.setText(f"Добавлено монет: {self.chosen.count()}")

    def commit(self):
        self.result_symbols = [
            self.chosen.item(i).text() for i in range(self.chosen.count())
        ]
        if not self.result_symbols:
            self.notice.setText("Выберите хотя бы одну монету")
            return
        self.accept()
