"""Mandatory native Qt acceptance for the unified Create/Edit/Open Draft path.

Not counted as passing unless executed with PySide6 and native Windows Qt.
"""
import os
if os.name != 'nt' and not os.environ.get('DISPLAY'):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtWidgets import QApplication

from bcc.config import defaults, resolve_poly_market
from bcc.constructor_edit_workspace import (
    edit_workspace_binding, preserve_published_constructor_workspace,
)
from bcc.constructor_session import (
    load_constructor_draft_snapshot, save_constructor_draft_with_revision,
)
from bcc.storage import Repository
from bcc.wizard import BotWizard


def test_native_qt_imported_draft_publishes_active_and_preserves_dormant_timing(tmp_path):
    app = QApplication.instance() or QApplication([])
    cfg = defaults('POLYMARKET')
    cfg['name'] = 'Imported before-close market timing'
    cfg['polymarket'].update(entry_signal_mode='before_close',
                              entry_seconds_before_close=100,
                              entry_window_seconds=15)
    cfg['polymarket']['overrides']['ETH:900'] = {
        'entry_seconds_before_close': 65, 'entry_window_seconds': 10}
    source = tmp_path / 'external.json'
    save_constructor_draft_with_revision(source, cfg, expected_revision=None)
    original = source.read_bytes()
    restored, revision = load_constructor_draft_snapshot(source)
    repo = Repository(tmp_path / 'repo')
    created = []
    wizard = BotWizard(restored, draft_path=source, draft_revision=revision,
                       on_save=lambda published: created.append(repo.create(published)))
    try:
        wizard.durations[900].setChecked(False)
        wizard.commit()
        assert created and wizard.result_workspace is not None
        saved = wizard.result_config
        assert 'ETH:900' not in saved['polymarket']['overrides']
        status = preserve_published_constructor_workspace(
            repo, created[0], wizard.result_workspace, saved)
        assert status['dormant_preserved']
        assert source.read_bytes() == original
        _, digest = repo.load(created[0])
        rebound, _ = load_constructor_draft_snapshot(
            status['draft_path'], binding=edit_workspace_binding(
                repo.root, created[0], digest))
        rebound['polymarket']['durations'].append(900)
        assert resolve_poly_market(rebound, 'ETH', 900)['entry_seconds_before_close'] == 65
        assert not (created[0] / 'paper.sqlite').exists()
    finally:
        wizard.close()
