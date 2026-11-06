"""Export must share the same OS owner fence as STOP, edits and ledger commits."""
import threading

import pytest

from bcc.config import defaults
from bcc.storage import Repository, Store
from bcc.supervisor import Supervisor


def test_snapshot_blocks_stop_until_snapshot_finishes(tmp_path, monkeypatch):
    repo = Repository(tmp_path / 'app')
    supervisor = Supervisor(repo)
    config = defaults('GRID')
    config['name'] = 'Export grid'
    bot = supervisor.create_bot(config)
    Store(bot, config['capital'])
    snapshot_open = threading.Event()
    allow_snapshot = threading.Event()
    stop_submitted = threading.Event()
    failures = []
    original_backup = Store.backup

    def paused_backup(store, target):
        snapshot_open.set()
        if not allow_snapshot.wait(8):
            raise TimeoutError('test backup gate stalled')
        return original_backup(store, target)

    monkeypatch.setattr(Store, 'backup', paused_backup)

    def export():
        try:
            supervisor.export_bots([bot], tmp_path / 'bots.zip', history=True)
        except BaseException as exc:
            failures.append(exc)

    def stop():
        try:
            supervisor.request_stop(bot)
            stop_submitted.set()
        except BaseException as exc:
            failures.append(exc)

    writer = threading.Thread(target=export)
    writer.start()
    try:
        assert snapshot_open.wait(8)
        stopper = threading.Thread(target=stop)
        stopper.start()
        try:
            # STOP cannot commit in the middle of the snapshot's gate.
            assert not stop_submitted.wait(0.15)
        finally:
            allow_snapshot.set()
            stopper.join(10)
        assert stop_submitted.is_set()
    finally:
        allow_snapshot.set()
        writer.join(10)
    assert not failures
    assert not writer.is_alive()
    assert (tmp_path / 'bots.zip').is_file()


def test_export_rejects_owner_being_retired(tmp_path):
    repo = Repository(tmp_path / 'app')
    supervisor = Supervisor(repo)
    config = defaults('VELES')
    config['name'] = 'Export veles'
    bot = supervisor.create_bot(config)
    supervisor.coordinator.begin_retirement(supervisor._key(bot))
    with pytest.raises(ValueError, match='удаляется'):
        supervisor.export_bots([bot], tmp_path / 'bots.zip', history=False)
    assert not (tmp_path / 'bots.zip').exists()
