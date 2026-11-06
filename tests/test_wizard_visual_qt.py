"""Windows Qt E2E: approved functional wizard layout, not screenshot matching.

These tests REQUIRE PySide6. Do not count them as passed on Linux without Qt.
"""

import os
if os.name != "nt" and not os.environ.get("DISPLAY"):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QTableWidgetItem
from bcc.config import defaults, validate
from bcc.ui import apply_style
from bcc.wizard import BotWizard


@pytest.fixture(scope="module")
def app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_functional_polymarket_tabs_and_seconds_roundtrip(app, theme):
    apply_style(app, {"theme": theme, "scale": 100})
    cfg = defaults("POLYMARKET")
    cfg["name"] = "Qt contract"
    wizard = BotWizard(cfg)
    wizard.show()
    wizard.steps.setCurrentRow(2)
    app.processEvents()
    assert wizard.poly_tabs.count() == 5
    assert wizard.steps.count() == 7
    assert "Дополнительно" in wizard.steps.item(5).text()
    assert [wizard.poly_tabs.tabText(i) for i in range(5)] == [
        "Основные условия", "Фильтры рынка", "Время входа",
        "Рынки и монеты", "Дополнительно",
    ]
    wizard.poly_tabs.setCurrentIndex(2)
    mode = wizard.poly.controls["entry_signal_mode"][1]
    mode.setCurrentIndex(mode.findData("before_close"))
    entry = wizard.poly.controls["entry_seconds_before_close"][1]
    entry.setValue(47.5)
    wizard.poly.controls["entry_window_seconds"][1].setValue(8)
    wizard.poly.controls["entry_min_seconds_left"][1].setValue(5)
    app.processEvents()
    assert entry.isVisible() and entry.isEnabled()
    assert wizard.poly_matrix.rowCount() == len(cfg["symbols"]) * len(cfg["polymarket"]["durations"])
    assert type(wizard.poly_matrix.item(0, 3)) is QTableWidgetItem
    assert "47.5" in wizard.summary.toPlainText() if hasattr(wizard.summary, "toPlainText") else "47.5" in wizard.summary.text()
    outgoing = validate(wizard.data())
    assert outgoing["polymarket"]["entry_seconds_before_close"] == 47.5
    assert outgoing["polymarket"]["entry_window_seconds"] == 8
    wizard.commit()
    assert wizard.result_config["polymarket"]["entry_seconds_before_close"] == 47.5
    reopened = BotWizard(wizard.result_config)
    assert reopened.poly.controls["entry_seconds_before_close"][1].value() == 47.5
    reopened.close()
    wizard.close()


def test_market_grid_native_numeric_sort_and_selection(app):
    apply_style(app, {"theme": "dark", "scale": 100})
    wizard = BotWizard(defaults("POLYMARKET"))
    wizard.steps.setCurrentRow(2)
    wizard.poly_tabs.setCurrentIndex(3)
    matrix = wizard.poly_matrix
    matrix.sortItems(1, Qt.SortOrder.DescendingOrder)
    values = [float(matrix.item(i, 1).text()) for i in range(matrix.rowCount())]
    assert values == sorted(values, reverse=True)
    assert all(type(matrix.item(i, 1)) is QTableWidgetItem for i in range(matrix.rowCount()))
    key = matrix.item(0, 0).data(Qt.ItemDataRole.UserRole + 1)
    wizard.update_summary()
    assert key == matrix.item(0, 0).data(Qt.ItemDataRole.UserRole + 1)
    wizard.close()


def _market_keys(table):
    return [table.item(row, 0).data(Qt.ItemDataRole.UserRole + 1)
            for row in range(table.rowCount())]


def test_market_grid_preserves_sort_and_selection_when_draft_values_change(app):
    """A new input must not silently redirect the edit action to another coin."""
    apply_style(app, {"theme": "dark", "scale": 100})
    wizard = BotWizard(defaults("POLYMARKET"))
    wizard.show()
    wizard.steps.setCurrentRow(2)
    wizard.poly_tabs.setCurrentIndex(3)
    app.processEvents()
    matrix = wizard.poly_matrix
    matrix.sortItems(1, Qt.SortOrder.DescendingOrder)
    before = _market_keys(matrix)
    assert before
    chosen = before[0]
    matrix.selectRow(0)

    # This edits a column other than the active duration sort, so even ties
    # (several 3600-second markets) must retain their visual order.
    # Default is 'anytime': editing dormant seconds must never switch the
    # trading mode implicitly. The rail shows them as not active.
    wizard.poly.controls["entry_seconds_before_close"][1].setValue(47.5)
    app.processEvents()
    assert "В любое время" in wizard.summary.text()
    assert "47.5" in wizard.summary.text()
    assert "не действует" in wizard.summary.text()
    # Switch explicitly to scheduled entry and verify the effective rule.
    mode = wizard.poly.controls["entry_signal_mode"][1]
    mode.setCurrentIndex(mode.findData("before_close"))
    app.processEvents()
    assert "За 47.5 сек" in wizard.summary.text()
    wizard.update_summary()
    assert _market_keys(matrix) == before
    assert matrix.item(matrix.currentRow(), 0).data(
        Qt.ItemDataRole.UserRole + 1) == chosen

    # The same identity must survive a change to the active numeric sort key.
    matrix.sortItems(3, Qt.SortOrder.AscendingOrder)
    matrix.selectRow(0)
    chosen = matrix.item(0, 0).data(Qt.ItemDataRole.UserRole + 1)
    wizard.poly.controls["entry_seconds_before_close"][1].setValue(52.25)
    wizard.update_summary()
    assert [float(matrix.item(row, 3).text())
            for row in range(matrix.rowCount())] == sorted(
                float(matrix.item(row, 3).text())
                for row in range(matrix.rowCount()))
    assert matrix.item(matrix.currentRow(), 0).data(
        Qt.ItemDataRole.UserRole + 1) == chosen
    wizard.close()


def test_market_row_identity_survives_add_or_remove_duration(app):
    apply_style(app, {"theme": "light", "scale": 100})
    wizard = BotWizard(defaults("POLYMARKET"))
    wizard.show()
    wizard.steps.setCurrentRow(2)
    wizard.poly_tabs.setCurrentIndex(3)
    app.processEvents()
    matrix = wizard.poly_matrix
    matrix.sortItems(1, Qt.SortOrder.DescendingOrder)
    chosen = next((r for r in range(matrix.rowCount())
                   if matrix.item(r, 0).data(Qt.ItemDataRole.UserRole + 1) == 'BTC:300'), None)
    assert chosen is not None
    matrix.selectRow(chosen)
    wizard.durations[900].setChecked(False)
    wizard.update_summary()
    assert all(not key.endswith(':900') for key in _market_keys(matrix))
    assert matrix.item(matrix.currentRow(), 0).data(
        Qt.ItemDataRole.UserRole + 1) == 'BTC:300'
    wizard.durations[900].setChecked(True)
    wizard.update_summary()
    assert 'BTC:900' in _market_keys(matrix)
    assert matrix.item(matrix.currentRow(), 0).data(
        Qt.ItemDataRole.UserRole + 1) == 'BTC:300'
    wizard.close()


def test_market_preview_and_stage_indicator_share_real_config(app):
    apply_style(app, {"theme":"dark", "scale":100})
    wizard = BotWizard(defaults("POLYMARKET"))
    wizard.show()
    wizard.steps.setCurrentRow(2)
    wizard.poly_tabs.setCurrentIndex(0)
    app.processEvents()
    assert wizard.step_counter.text() == "Шаг 3 из 7"
    assert "В любое время" in wizard.poly_rule_preview.text()
    mode = wizard.poly.controls["entry_signal_mode"][1]
    mode.setCurrentIndex(mode.findData("before_close"))
    entry = wizard.poly.controls["entry_seconds_before_close"][1]
    entry.setValue(47.5)
    app.processEvents()
    assert "от 47.5 до 22.5 сек" in wizard.poly_rule_preview.text()
    assert "За 47.5 сек" in wizard.summary.text()
    wizard.steps.setCurrentRow(6)
    assert wizard.step_counter.text() == "Шаг 7 из 7"
    wizard.close()


def test_approved_seven_step_visual_navigation_and_real_controls(app):
    """Step indicator follows QStackedWidget, never swaps in mock pages."""
    apply_style(app, {"theme": "dark", "scale": 100})
    wizard = BotWizard(defaults("POLYMARKET"))
    wizard.show()
    app.processEvents()
    assert wizard.steps.count() == wizard.pages.count() == 7
    assert wizard.steps.itemDelegate().__class__.__name__ == "WizardStepDelegate"
    for index in (1, 2, 5, 6, 2):
        wizard.steps.setCurrentRow(index)
        app.processEvents()
        assert wizard.pages.currentIndex() == index
        assert wizard.step_counter.text() == f"Шаг {index + 1} из 7"
        assert index in wizard._visited_steps
    assert wizard.step_note.text() == "Просмотрено: 5 из 7"
    wizard.steps.setCurrentRow(2)
    wizard.poly_tabs.setCurrentIndex(1)
    control = wizard.poly.controls["ask_min"][1]
    control.setValue(0.12)
    app.processEvents()
    assert wizard.data()["polymarket"]["ask_min"] == pytest.approx(0.12)
    wizard.close()


def test_approved_stepper_light_and_dark_uses_same_config(app):
    initial = defaults("POLYMARKET")
    for theme in ("dark", "light"):
        apply_style(app, {"theme": theme, "scale": 100})
        wizard = BotWizard(initial)
        wizard.show()
        wizard.steps.setCurrentRow(2)
        app.processEvents()
        assert wizard.steps.item(2).data(Qt.ItemDataRole.UserRole)["title"] == "Условия входа"
        assert wizard.poly_rule_preview.text()
        assert wizard.data()["polymarket"] == initial["polymarket"]
        wizard.close()


def test_dashboard_actions_are_real_lifecycle_controls(app, tmp_path):
    """R5 groups actual action callbacks, not inert visual replicas."""
    from PySide6.QtWidgets import QFrame, QPushButton
    from bcc.ui import MainWindow
    apply_style(app, {"theme": "dark", "scale": 100})
    dashboard = MainWindow(tmp_path / "acceptance_root")
    area = dashboard.findChild(QFrame, "dashboardActionArea")
    assert area is not None
    actions = {item.text() for item in area.findChildren(QPushButton)}
    assert {"▶ Запуск", "■ Стоп", "Редактировать", "Просмотр параметров",
            "Удалить", "Сохранить отчёт", "Монитор всех ботов"} <= actions
    assert dashboard.findChild(QPushButton, "openRunningBots") is not None
    assert dashboard.findChild(QPushButton, "saveCurrentBotsReport") is not None
    dashboard.close()
