"""Native Qt create/edit/persist contract; required on Windows before release.

No workers are started and no orders are placed. This module is intentionally
not counted as passing when PySide6/Windows acceptance is unavailable.
"""
import os
if os.name != 'nt' and not os.environ.get('DISPLAY'):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtWidgets import QApplication
from bcc.config import defaults
from bcc.storage import Repository
from bcc.wizard import BotWizard


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


def test_polymarket_qt_publish_excludes_dormant_and_preserves_seconds(app, tmp_path):
    cfg = defaults('POLYMARKET')
    cfg['name'] = 'Qt full market publication'
    cfg['polymarket']['overrides']['ETH:900'] = {
        'entry_seconds_before_close': 75, 'entry_window_seconds': 8}
    repo = Repository(tmp_path)
    published = []

    def save(candidate):
        published.append(repo.create(candidate))

    wizard = BotWizard(cfg, on_save=save)
    try:
        wizard.poly.controls['entry_signal_mode'][1].setCurrentIndex(
            wizard.poly.controls['entry_signal_mode'][1].findData('before_close'))
        wizard.poly.controls['entry_seconds_before_close'][1].setValue(110)
        wizard.poly.controls['entry_window_seconds'][1].setValue(10)
        wizard.durations[900].setChecked(False)
        assert 'ETH:900' in wizard.data(include_inactive=True)['polymarket']['overrides']
        wizard.commit()
        assert published
        saved, digest = repo.load(published[0])
        assert saved['polymarket']['entry_seconds_before_close'] == 110
        assert 'ETH:900' not in saved['polymarket']['overrides']
    finally:
        wizard.close()

    reopened = BotWizard(saved, on_save=lambda c: repo.save(published[0], c, digest))
    try:
        reopened.poly.controls['entry_seconds_before_close'][1].setValue(95)
        reopened.commit()
        updated, _ = repo.load(published[0])
        assert updated['polymarket']['entry_seconds_before_close'] == 95
    finally:
        reopened.close()


def test_veles_qt_create_then_edit_with_independent_coin_risk(app, tmp_path):
    cfg = defaults('VELES')
    cfg['name'] = 'Qt full VELES publication'
    cfg['coin_overrides']['ETHUSDT'] = {'settings': {'leverage': 6}}
    repo = Repository(tmp_path)
    published = []
    wizard = BotWizard(cfg, on_save=lambda c: published.append(repo.create(c)))
    try:
        wizard.commit()
        assert published
    finally:
        wizard.close()
    saved, digest = repo.load(published[0])
    assert saved['coin_overrides']['ETHUSDT']['settings']['leverage'] == 6
    reopened = BotWizard(saved, on_save=lambda c: repo.save(published[0], c, digest))
    try:
        reopened.capital.setValue(700)
        reopened.commit()
        updated, _ = repo.load(published[0])
        assert updated['capital'] == 700
        assert updated['coin_overrides']['ETHUSDT']['settings']['leverage'] == 6
    finally:
        reopened.close()
