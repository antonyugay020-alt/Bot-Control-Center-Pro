"""Native Qt regression: a conditional order-grid preview must not erase the change audit.

This module is intentionally part of the native Qt suite and is not counted as
headless acceptance when PySide6 or a display backend is unavailable.
"""
import os
if os.name != 'nt' and not os.environ.get('DISPLAY'):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtWidgets import QApplication
from bcc.config import defaults
from bcc.wizard import BotWizard


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


def test_veles_review_and_grid_preview_are_independent_on_reselection(app):
    config = defaults('VELES')
    config.update(name='Review isolation', symbols=['BTCUSDT', 'ETHUSDT'])
    wizard = BotWizard(config)
    try:
        wizard.steps.setCurrentRow(wizard.steps.count() - 1)
        wizard.capital.setValue(config['capital'] + 125)
        wizard.update_summary()
        initial = wizard.review.text()
        assert 'Изменения относительно открытой конфигурации' in initial
        assert 'capital' in initial
        assert 'Маржа из исходного депозита' in wizard.preview_status.text()
        assert wizard.preview_status.objectName() == 'wizardGridPreviewStatus'
        wizard.preview_coin.setCurrentIndex(1)
        wizard.update_summary()
        assert wizard.review.text() == initial
        assert 'ETHUSDT' in wizard.preview_status.text()
        assert 'Изменения относительно' not in wizard.preview_status.text()
    finally:
        wizard.close()


def test_polymarket_change_review_survives_without_order_grid(app):
    config = defaults('POLYMARKET')
    config.update(name='Timing review', symbols=['BTC', 'ETH'])
    wizard = BotWizard(config)
    try:
        wizard.steps.setCurrentRow(wizard.steps.count() - 1)
        widget = wizard.poly.controls['entry_signal_mode'][1]
        idx = widget.findData('before_close')
        assert idx >= 0
        widget.setCurrentIndex(idx)
        wizard.poly.controls['entry_seconds_before_close'][1].setValue(55)
        wizard.update_summary()
        assert 'Изменён вход за N секунд' in wizard.review.text()
        assert wizard.preview_status.isHidden()
        assert wizard.data()['polymarket']['entry_seconds_before_close'] == 55
    finally:
        wizard.close()
