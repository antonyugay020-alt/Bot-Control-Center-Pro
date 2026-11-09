"""Native Qt controls for coin-budget modes and persisted VELES settings."""
import os
if os.name != 'nt' and not os.environ.get('DISPLAY'):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtWidgets import QApplication
from bcc.config import defaults, validate
from bcc.widgets import SettingsEditor
from bcc.wizard import BotWizard


def test_veles_coin_budget_mode_switch_persists_in_wizard():
    app = QApplication.instance() or QApplication([])
    cfg = defaults('VELES')
    cfg['name'] = 'Coin budget acceptance'
    wizard = BotWizard(cfg)
    try:
        mode = wizard.settings.finance.controls['allocation_mode'][1]
        amount = wizard.settings.finance.controls['allocation_usdt'][1]
        pct = wizard.settings.finance.controls['allocation_pct'][1]
        assert not amount.isEnabled()
        mode.setCurrentIndex(mode.findData('coin_fixed'))
        assert amount.isEnabled() and not pct.isEnabled()
        amount.setValue(80.25)
        saved = validate(wizard.data())
        assert saved['settings']['allocation_mode'] == 'coin_fixed'
        assert saved['settings']['allocation_usdt'] == 80.25
        reopened = BotWizard(saved)
        try:
            assert reopened.settings.finance.value_data()['allocation_usdt'] == 80.25
            mode2 = reopened.settings.finance.controls['allocation_mode'][1]
            mode2.setCurrentIndex(mode2.findData('coin_pct'))
            reopened.settings.finance.controls['allocation_pct'][1].setValue(6.5)
            revised = validate(reopened.data())
            assert revised['settings']['allocation_mode'] == 'coin_pct'
            assert revised['settings']['allocation_pct'] == 6.5
        finally:
            reopened.close()
    finally:
        wizard.close()


def test_coin_editor_schema_exposes_same_budget_for_individual_coin():
    app = QApplication.instance() or QApplication([])
    cfg = defaults('VELES')
    editor = SettingsEditor(cfg['settings'], strategy='VELES')
    try:
        mode = editor.finance.controls['allocation_mode'][1]
        mode.setCurrentIndex(mode.findData('coin_fixed'))
        editor.finance.controls['allocation_usdt'][1].setValue(45.0)
        values = editor.value_data()
        assert values['allocation_mode'] == 'coin_fixed'
        assert values['allocation_usdt'] == 45
    finally:
        editor.close()
