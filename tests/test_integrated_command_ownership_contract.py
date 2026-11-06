"""Architecture boundary: GUI/operator intent, service, and ledger ownership."""
from pathlib import Path

import pytest

from bcc.config import defaults
from bcc.coordination import Coordinator
from bcc.lifecycle import Lifecycle
from bcc.runtime import close_requested_positions
from bcc.storage import BotBusy, Repository, Store
from bcc.supervisor import Supervisor


def test_no_legacy_manual_close_bypass_and_durable_operator_delivery(tmp_path):
    repo = Repository(tmp_path / 'state')
    cfg = defaults('VELES')
    cfg['name'] = 'Authority integration'
    folder = repo.create(cfg)
    sup = Supervisor(repo)
    with pytest.raises(BotBusy, match='Supervisor.submit_manual_close'):
        Lifecycle(repo).close_positions(folder)
    assert not (folder / 'close.request').exists()
    sup.submit(folder, running=True)
    key = sup._key(folder)
    epoch = sup.coordinator.get_registration(key)['command_version']
    sup.submit_manual_close(folder)
    assert not (folder / 'close.request').exists()
    store = Store(folder, cfg['capital'])
    close_requested_positions(folder, store, {}, control_revision=epoch)
    assert sup.coordinator.pending_manual_close(key, launch_revision=epoch) is None
    assert store.read(False)['strategy']['last_close_command'] == 'control-close:1'
    store.audit()


def test_manual_close_ack_uses_registry_owner_not_position_id(tmp_path, monkeypatch):
    from contextlib import contextmanager
    import bcc.runtime as runtime

    repo = Repository(tmp_path / 'state')
    cfg = defaults('VELES')
    cfg['name'] = 'Independent account owner'
    folder = repo.create(cfg)
    sup = Supervisor(repo)
    sup.submit(folder, running=True)
    owner_key = sup._key(folder)
    epoch = sup.coordinator.get_registration(owner_key)['command_version']
    sup.submit_manual_close(folder)

    state = {'strategy': {}, 'positions': {
        'BTCUSDT:main': {'symbol': 'BTCUSDT', 'side': 'LONG', 'average': 100}},
        'pending': {}, 'marks': {}}
    class CommittingStore:
        @contextmanager
        def transaction(self, **kwargs):
            yield state, []

    def close(state, events, position_key, price, reason):
        assert position_key == 'BTCUSDT:main'
        del state['positions'][position_key]
    monkeypatch.setattr(runtime, 'exit_position', close)
    runtime.close_requested_positions(folder, CommittingStore(), {}, control_revision=epoch)
    assert sup.coordinator.pending_manual_close(owner_key, launch_revision=epoch) is None
    assert state['strategy']['last_close_command'] == 'control-close:1'
