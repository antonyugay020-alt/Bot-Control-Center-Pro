"""Safe viewing of active/start/disk and frozen position configurations."""

import json
from datetime import datetime

from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from .config import is_secret_key
from .storage import atomic_json
from .widgets import button, dialog_size, label

NAMES = {
    "liquidation": "Текущее сокращение риска",
    "liquidation_error": "Причина ожидания сокращения риска",
    "last_liquidation": "Последнее завершённое сокращение риска",
    "target_remaining_qty": "Целевой остаток позиции",
    "initial_qty": "Объём до начала сокращения риска",
    "from_tier": "Исходная ступень риска",
    "to_tier": "Целевая ступень риска",
    "margin_evidence": "Параметры последнего расчёта маржи",
    "margin_model": "Финансовая модель",
    "margin_data": "Публичные данные маржи",
    "margin_data_error": "Ошибка публичных данных маржи",
    "mark_price": "Mark-price",
    "mark_at": "Время mark-price UTC, Unix",
    "tier_id": "Ступень риска",
    "maintenance_rate": "Ставка maintenance margin, доля",
    "deduction": "Вычет maintenance margin, USDT",
    "maintenance": "Поддерживающая маржа, USDT",
    "fee_to_close": "Резерв комиссии закрытия, USDT",
    "initial": "Начальное обеспечение, USDT",
    "unrealized": "Нереализованный результат по mark-price, USDT",
    "entry_signal_mode": "Режим входа",
    "entry_seconds_before_close": "Начать вход за … секунд до конца",
    "entry_window_seconds": "Длительность окна входа, секунд",
    "entry_min_seconds_left": "Минимальный остаток времени, секунд",
    "strategy_params": "Применённые параметры стратегии при входе",
    "strategy_parameters": "Применённые параметры стратегии при входе",
    "entry_ts_utc": "Время входа UTC",
    "seconds_into_market": "Секунд от начала рынка",
    "seconds_left": "Секунд до конца при входе",
    "entry_impulse": "Импульс при входе",
    "entry_ask": "Лучший ASK при входе",
    "execution_policy": "PAPER-исполнение",
    "name": "Название",
    "strategy": "Стратегия",
    "mode": "Режим",
    "symbols": "Монеты",
    "capital": "Общий депозит",
    "settings": "Торговые параметры",
    "coin_overrides": "Индивидуальные параметры монет",
    "sections": "Секции",
    "entry": "Вход",
    "exit": "Выход",
    "orders": "Ордера и DCA",
    "profit": "Тейк-профит",
    "stop": "Стоп-лосс",
    "period": "Период",
    "timeframe": "Таймфрейм, минут",
    "trigger": "Момент сигнала",
    "indicator": "Индикатор",
    "comparison": "Условие",
    "value": "Уровень",
    "reference": "Сравнивать с",
    "leverage": "Плечо",
    "margin_mode": "Маржа",
    "allocation_pct": "Доля капитала, %",
    "fee_rate": "Комиссия, доля",
    "slippage_pct": "Проскальзывание, %",
    "disabled": "Новые входы отключены",
    "stop_after_cycle": "Остановить после сделки",
    "reinvest": "Реинвестирование",
    "stop_after_deals": "Завершить работу бота после сделок",
    "draining": "Новые сделки отключены; ожидается завершение текущих",
    "finish_on_close": "После следующего выхода перейти к завершению",
    "reinvest_pct": "Доля чистой прибыли для реинвестирования, %",
    "veles_reinvested": "Добавлено в депозит из завершённых сделок, USDT",
    "stop_after": "Остановка секции после стоп-лосса",
    "side": "Направление",
    "label": "Название секции",
    "enabled": "Включена",
    "operator": "Логика группы",
    "conditions": "Условия",
    "groups": "Группы",
    "overrides": "Переопределения",
    "pct": "Процент",
    "minimum_pct": "Минимальный PnL, %",
    "count": "Количество ордеров",
    "coverage_pct": "Перекрытие, %",
    "martingale_pct": "Мартингейл, %",
    "first_offset_pct": "Первый отступ, %",
    "logarithmic": "Логарифмический коэффициент",
    "partial_count": "Активных ордеров",
    "pull_up_pct": "Подтяжка, %",
    "custom": "Свои ордера",
    "volume_pct": "Объём, %",
    "offset_pct": "Отступ, %",
    "parameters": "Параметры стратегии",
    "grid": "GRID",
    "pump": "PUMP&DUMP",
    "polymarket": "Polymarket",
    "indicator_filter": "Фильтр RSI / EMA / Объём",
    "indicator_evidence": "Подтверждение фильтра при PAPER-входе",
    "available": "Достоверные свечи получены",
    "matched": "Фильтр выполнен",
    "observations": "Значения индикаторов при входе",
    "last_candle_start_ms": "Последняя закрытая свеча, UTC мс",
    "reference_period": "Второй период",
    "reference_timeframe": "Второй таймфрейм",
    "shift": "Сдвиг",
    "reference_shift": "Второй сдвиг",
}


def safe(value):
    if isinstance(value, dict):
        return {
            k: ("[скрыто]" if is_secret_key(k) else safe(v)) for k, v in value.items()
        }
    if isinstance(value, list):
        return [safe(x) for x in value]
    return value


def tree(data):
    w = QTreeWidget()
    w.setHeaderLabels(["Параметр", "Значение"])
    w.header().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)

    def insert(parent, key, value):
        item = QTreeWidgetItem(
            [
                NAMES.get(str(key), str(key)),
                (
                    ""
                    if isinstance(value, (dict, list))
                    else (
                        "Да"
                        if value is True
                        else (
                            "Нет"
                            if value is False
                            else "Выключено" if value is None else str(value)
                        )
                    )
                ),
            ]
        )
        if parent is None:
            w.addTopLevelItem(item)
        else:
            parent.addChild(item)
        if isinstance(value, dict):
            for k, v in value.items():
                insert(item, k, v)
        elif isinstance(value, list):
            for i, v in enumerate(value):
                insert(item, str(i + 1), v)

    for key, value in data.items():
        insert(None, key, value)
    w.expandToDepth(1)
    return w


def inspect_settings(parent, documents, notice, *, refresh=None):
    dialog = QDialog(parent)
    dialog.setWindowTitle("Все настройки бота")
    dialog_size(dialog, 1150, 800)
    layout = QVBoxLayout(dialog)
    source_label = label(notice, "muted")
    layout.addWidget(source_label)
    tabs = QTabWidget()
    layout.addWidget(tabs)
    state = {}

    def render(documents, notice):
        selected = tabs.tabText(tabs.currentIndex())
        while tabs.count():
            previous = tabs.widget(0)
            tabs.removeTab(0)
            previous.deleteLater()
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        state["documents"] = {title: safe(data) for title, data in documents.items()}
        state["documents"]["Сведения об отчёте"] = {
            "Время снимка": now,
            "Источник": notice,
        }
        for title, data in state["documents"].items():
            tabs.addTab(tree(data), title)
            if title == selected:
                tabs.setCurrentWidget(tabs.widget(tabs.count() - 1))
        source_label.setText(notice + " · снимок " + now)

    render(documents, notice)
    actions = QHBoxLayout()
    actions.addWidget(
        button(
            "Копировать безопасный отчёт",
            lambda: QApplication.clipboard().setText(
                json.dumps(state["documents"], ensure_ascii=False, indent=2)
            ),
        )
    )

    def export():
        file, _ = QFileDialog.getSaveFileName(
            dialog, "Отчёт параметров", "bot_settings.json", "JSON (*.json)"
        )
        if file:
            atomic_json(file, state["documents"])

    actions.addWidget(button("Экспорт отчёта", export))
    if refresh:

        def update():
            try:
                render(*refresh())
            except (OSError, ValueError, KeyError, RuntimeError) as e:
                source_label.setText(
                    "Обновление недоступно; показан предыдущий снимок: " + str(e)
                )

        refresh_button = button("Обновить", update)
        refresh_button.setObjectName("refreshSettingsReport")
        actions.addWidget(refresh_button)
    actions.addStretch()
    layout.addLayout(actions)
    controls = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    controls.rejected.connect(dialog.reject)
    layout.addWidget(controls)
    dialog.exec()
    dialog.deleteLater()


def changes(before, after, prefix=""):
    if isinstance(before, dict) and isinstance(after, dict):
        result = []
        for k in before.keys() | after.keys():
            result.extend(
                changes(before.get(k), after.get(k), prefix + " / " + NAMES.get(k, k))
            )
        return result
    if before == after:
        return []
    return [
        prefix.strip(" /")
        + ": "
        + json.dumps(before, ensure_ascii=False)
        + " → "
        + json.dumps(after, ensure_ascii=False)
    ]
