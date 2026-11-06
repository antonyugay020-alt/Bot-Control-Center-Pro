"""Native Qt E2E for lossless draft workspace vs publishable config."""
import os
if os.name!='nt' and not os.environ.get('DISPLAY'):
    os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
from PySide6.QtWidgets import QApplication
from bcc.config import defaults
from bcc.constructor_session import load_constructor_draft
from bcc.wizard import BotWizard

@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


def test_polymarket_deselected_duration_keeps_market_entry_seconds_in_draft(app,tmp_path):
    cfg=defaults('POLYMARKET')
    cfg['symbols']=['BTC','ETH']
    cfg['polymarket']['entry_signal_mode']='before_close'
    cfg['polymarket']['overrides']['ETH:900']={'entry_seconds_before_close':47.5}
    path=tmp_path/'draft_poly.json'
    wizard=BotWizard(cfg,draft_path=path)
    try:
        assert 900 in wizard.durations
        wizard.durations[900].setChecked(False)
        assert 'ETH:900' not in wizard.data()['polymarket']['overrides']
        assert wizard.data(include_inactive=True)['polymarket']['overrides']['ETH:900']['entry_seconds_before_close']==47.5
        wizard.save_draft()
        assert load_constructor_draft(path)['polymarket']['overrides']['ETH:900']['entry_seconds_before_close']==47.5
    finally:
        wizard.close()
    reopened=BotWizard(load_constructor_draft(path))
    try:
        reopened.durations[900].setChecked(True)
        assert reopened.data()['polymarket']['overrides']['ETH:900']['entry_seconds_before_close']==47.5
    finally:
        reopened.close()


def test_veles_deselected_coin_keeps_individual_leverage_in_draft(app,tmp_path):
    cfg=defaults('VELES')
    cfg['symbols']=['BTCUSDT','ETHUSDT']
    cfg['coin_overrides']['ETHUSDT']={'settings':{'leverage':7}}
    path=tmp_path/'draft_veles.json'
    wizard=BotWizard(cfg,draft_path=path)
    try:
        wizard.symbols.setText('BTCUSDT')
        assert 'ETHUSDT' not in wizard.data()['coin_overrides']
        wizard.save_draft()
        assert load_constructor_draft(path)['coin_overrides']['ETHUSDT']['settings']['leverage']==7
    finally:
        wizard.close()
    reopened=BotWizard(load_constructor_draft(path))
    try:
        reopened.symbols.setText('BTCUSDT, ETHUSDT')
        assert reopened.data()['coin_overrides']['ETHUSDT']['settings']['leverage']==7
    finally:
        reopened.close()
