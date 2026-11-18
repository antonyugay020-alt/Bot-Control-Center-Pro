"""Qt E2E for VELES coin-wide percent/fixed USDT capital allocation.

Creates only a local in-memory wizard; no PAPER workers or exchanges.
Native Windows execution is required before marking the UI feature accepted.
"""
import os
if os.name != "nt" and not os.environ.get("DISPLAY"):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QDoubleSpinBox,
)

from bcc.config import defaults, validate
from bcc.coin_budget import limit_amount
from bcc.wizard import BotWizard


@pytest.fixture(scope="module")
def app():
    result = QApplication.instance() or QApplication([])
    yield result


def new_wizard():
    cfg = defaults("VELES")
    cfg["name"] = "VELES independent per-coin capital"
    return BotWizard(cfg)


def dialog_controls(app):
    dialog = next(
        widget for widget in app.topLevelWidgets()
        if isinstance(widget, QDialog)
        and widget.windowTitle() == "Индивидуальные настройки"
    )
    coin = dialog.findChild(QComboBox, "coinChoice")
    mode = dialog.findChild(QComboBox, "coinCapitalLimitMode")
    amount = dialog.findChild(QDoubleSpinBox, "coinCapitalLimitValue")
    individual = dialog.findChild(QCheckBox, "coinIndividual")
    box = dialog.findChild(QDialogButtonBox)
    assert all((coin, mode, amount, individual, box))
    return dialog, coin, mode, amount, individual, box


def run_coin_dialog(app, wizard, operation):
    """Never hang a native Windows Qt modal if an assertion fails in QTimer.

    PySide signal callbacks do not propagate their exceptions through
    QDialog.exec(). Capture, close ONLY the test dialog, then re-raise.
    """
    failures = []

    def invoke():
        dialog = app.activeModalWidget()
        try:
            operation()
        except BaseException as exc:
            failures.append(exc)
            if isinstance(dialog, QDialog):
                dialog.reject()

    QTimer.singleShot(0, invoke)
    wizard.edit_coin()
    if failures:
        raise failures[0]


def test_veles_qt_coin_budget_applies_to_two_coins_and_reopens(app):
    wizard = new_wizard()
    wizard.show()
    app.processEvents()

    def modify():
        dialog, coin, mode, amount, individual, box = dialog_controls(app)
        assert coin.currentData() == "BTCUSDT"
        assert mode.currentData() == "inherit"
        assert not individual.isChecked()
        assert not amount.isEnabled()

        mode.setCurrentIndex(mode.findData("percent"))
        amount.setValue(7.5)
        assert individual.isChecked() and amount.isEnabled()

        coin.setCurrentIndex(coin.findData("ETHUSDT"))
        assert mode.currentData() == "inherit"
        mode.setCurrentIndex(mode.findData("fixed"))
        amount.setValue(37.25)
        assert individual.isChecked()
        box.button(QDialogButtonBox.StandardButton.Save).click()
        assert dialog.result() == QDialog.DialogCode.Accepted

    run_coin_dialog(app, wizard, modify)
    config = validate(wizard.data())
    assert config["coin_overrides"]["BTCUSDT"]["capital_limit"] == {
        "mode": "percent", "value": 7.5}
    assert config["coin_overrides"]["ETHUSDT"]["capital_limit"] == {
        "mode": "fixed", "value": 37.25}
    assert limit_amount(config, "BTCUSDT") == 37.5
    assert limit_amount(config, "ETHUSDT") == 37.25

    # Reopening a complete Qt wizard must show the same persisted amounts,
    # not reset them from a global financing tab.
    reopened = BotWizard(config)
    reopened.show()
    app.processEvents()

    def inspect():
        dialog, coin, mode, amount, individual, box = dialog_controls(app)
        assert mode.currentData() == "percent"
        assert amount.value() == 7.5
        coin.setCurrentIndex(coin.findData("ETHUSDT"))
        assert mode.currentData() == "fixed"
        assert amount.value() == 37.25
        box.button(QDialogButtonBox.StandardButton.Cancel).click()

    run_coin_dialog(app, reopened, inspect)
    reopened.close()
    wizard.close()


def test_qt_reset_to_inherited_cap_keeps_other_coin_unchanged(app):
    cfg = defaults("VELES")
    cfg["name"] = "VELES budget reset"
    cfg["coin_overrides"] = {
        "BTCUSDT": {"capital_limit": {"mode": "fixed", "value": 55.0}},
        "ETHUSDT": {"capital_limit": {"mode": "percent", "value": 15.0}},
    }
    wizard = BotWizard(cfg)
    wizard.show()
    app.processEvents()

    def reset():
        dialog, coin, mode, amount, individual, box = dialog_controls(app)
        assert mode.currentData() == "fixed"
        assert amount.value() == 55.0
        mode.setCurrentIndex(mode.findData("inherit"))
        assert not amount.isEnabled()
        box.button(QDialogButtonBox.StandardButton.Save).click()

    run_coin_dialog(app, wizard, reset)
    result = validate(wizard.data())
    assert "capital_limit" not in result["coin_overrides"].get("BTCUSDT", {})
    assert result["coin_overrides"]["ETHUSDT"]["capital_limit"] == {
        "mode": "percent", "value": 15.0}
    wizard.close()


def test_qt_percent_mode_clamps_fixed_usdt_before_save(app):
    wizard = new_wizard()
    wizard.show()
    app.processEvents()

    def edit():
        dialog, coin, mode, amount, individual, box = dialog_controls(app)
        mode.setCurrentIndex(mode.findData("fixed"))
        amount.setValue(350)
        mode.setCurrentIndex(mode.findData("percent"))
        assert amount.maximum() == 100
        assert amount.value() == 100
        box.button(QDialogButtonBox.StandardButton.Save).click()

    run_coin_dialog(app, wizard, edit)
    saved = validate(wizard.data())
    assert saved["coin_overrides"]["BTCUSDT"]["capital_limit"] == {
        "mode": "percent", "value": 100.0}
    wizard.close()
