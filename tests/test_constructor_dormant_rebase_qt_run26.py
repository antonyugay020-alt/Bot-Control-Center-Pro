"""Native Qt constructor retention acceptance (mandatory before Windows release)."""
import os
if os.name != 'nt' and not os.environ.get('DISPLAY'):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtWidgets import QApplication

from bcc.config import defaults, resolve_poly_market
from bcc.constructor_edit_workspace import (
    edit_workspace_binding, retain_workspace_after_commit,
)
from bcc.constructor_session import load_constructor_draft_snapshot
from bcc.storage import Repository
from bcc.wizard import BotWizard


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


def test_qt_polymarket_create_reopen_retains_inactive_market_timing(app, tmp_path):
    cfg = defaults('POLYMARKET')
    cfg['name'] = 'Qt dormant timing lifecycle'
    cfg['polymarket'].update(entry_signal_mode='before_close',
                              entry_seconds_before_close=100,
                              entry_window_seconds=20)
    cfg['polymarket']['overrides']['ETH:900'] = {
        'entry_seconds_before_close': 75, 'entry_window_seconds': 8}
    repo = Repository(tmp_path)
    created = []
    wizard = BotWizard(cfg, on_save=lambda proposed: created.append(repo.create(proposed)))
    try:
        wizard.durations[900].setChecked(False)
        wizard.commit()
        assert created and wizard.result_workspace is not None
        published = wizard.result_config
        assert 'ETH:900' not in published['polymarket']['overrides']
        status = retain_workspace_after_commit(
            repo, created[0], wizard.result_workspace, published)
        assert status['dormant_preserved']
        _, digest = repo.load(created[0])
        draft, _ = load_constructor_draft_snapshot(
            status['draft_path'], binding=edit_workspace_binding(
                repo.root, created[0], digest))
        assert draft['polymarket']['overrides']['ETH:900']['entry_seconds_before_close'] == 75
        draft['polymarket']['durations'].append(900)
        assert resolve_poly_market(draft, 'ETH', 900)['entry_seconds_before_close'] == 75
    finally:
        wizard.close()
