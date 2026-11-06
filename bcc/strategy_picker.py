"""Modern Qt strategy chooser for creating a real bot, not a preview mode.

Each tile opens the SAME BotWizard and repository/worker path as creation
from a strategy-filtered Dashboard. Only declared strategy keys are emitted.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QGridLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

from .config import STRATEGIES, STRATEGY_NAMES


EXPLANATIONS = {
    "VELES": "Индикаторы и составные сигналы, диапазоны RSI, DCA, TP/SL, Cross/Isolated",
    "POLYMARKET": "PAPER: ASK, импульсы и время входа в секундах; параметры каждой монеты и рынка",
    "PUMP_DUMP": "Поиск импульсов и объёма, быстрый или подтверждённый отскок",
    "GRID": "Сетка ордеров по цене, шаги, TP/SL каждого уровня",
    "EMA": "Вход и выход по условиям экспоненциальных средних",
    "MACD": "Сигналы MACD с независимыми условиями входа и выхода",
    "BOLLINGER": "Полосы Боллинджера и индивидуальные правила сигналов",
    "EMA_RSI_VOLUME": "Составные сигналы EMA + RSI + относительный объём",
}


class StrategyPicker(QDialog):
    """Choose a strategy without using the unstyled system item dialog."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.selected_strategy = None
        self.setObjectName("strategyPicker")
        self.setWindowTitle("Создать бота · Modern Qt")
        self.resize(900, 650)
        root = QVBoxLayout(self)
        root.setSpacing(14)
        heading = QLabel("Выберите стратегию")
        heading.setObjectName("headline")
        root.addWidget(heading)
        note = QLabel("Настройка, сохранение, запуск и мониторинг доступны через единый конструктор PAPER-ботов.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        root.addWidget(note)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        container = QWidget()
        container.setProperty("scrollContent", True)
        grid = QGridLayout(container)
        grid.setSpacing(12)
        for i, strategy in enumerate(STRATEGIES):
            card = QWidget()
            card.setObjectName("strategyTile")
            card.setMinimumHeight(138)
            card_layout = QVBoxLayout(card)
            title = QLabel(STRATEGY_NAMES[strategy])
            title.setObjectName("title")
            card_layout.addWidget(title)
            desc = QLabel(EXPLANATIONS[strategy])
            desc.setWordWrap(True)
            desc.setObjectName("muted")
            card_layout.addWidget(desc, 1)
            choose = QPushButton("Настроить бота")
            choose.setObjectName("strategyChoose")
            choose.setProperty("strategy", strategy)
            choose.clicked.connect(lambda _checked=False, s=strategy: self.choose(s))
            card_layout.addWidget(choose, alignment=Qt.AlignmentFlag.AlignRight)
            grid.addWidget(card, i // 2, i % 2)
        scroll.setWidget(container)
        root.addWidget(scroll, 1)
        bottom = QHBoxLayout()
        bottom.addStretch()
        cancel = QPushButton("Отмена")
        cancel.clicked.connect(self.reject)
        bottom.addWidget(cancel)
        root.addLayout(bottom)

    def choose(self, strategy):
        if strategy not in STRATEGIES:
            raise ValueError("Неизвестная стратегия")
        self.selected_strategy = strategy
        self.accept()
