"""Control-plane retirement and PAPER filesystem deletion form one safe lifecycle."""
from pathlib import Path

import pytest

from bcc.config import defaults
from bcc.storage import Repository
from bcc.supervisor import Supervisor


def config():
    cfg = defaults('VELES')
    cfg['name'] = 'System delete test'
    return cfg


def test_delete_revokes_queued_run_and_removes_all_controller_state(tmp_path):
    repo = Repository(tmp_path)
    folder = repo.create(config())
    sup = Supervisor(repo)
    sup.request_start(folder)
    key = sup._key(folder)
    sup.coordinator.request_manual_close(key)
    sup.coordinator.set_drain(key, True)
    assert sup.coordinator.get_registration(key)['desired'] == 'RUNNING'
    sup.delete_bot(folder, discard_paper_state=True)
    assert not folder.exists()
    assert sup.coordinator.get_registration(key) is None
    with sup.coordinator.connection() as db:
        for table in ('manual_close_commands', 'drain_commands',
                      'command_observations', 'restart_policy'):
            assert not db.execute(f'SELECT 1 FROM {table} WHERE key=?', (key,)).fetchone()
    assert key not in sup.coordinator.next_keys(limit=20)
    # A newly created instance receives its own STOPPED default, never the
    # revoked RUNNING intent or pending PAPER command of the deleted account.
    replacement = repo.create(config())
    replacement_key = sup.register(replacement)
    assert sup.coordinator.get_registration(replacement_key)['desired'] == 'STOPPED'


def test_failed_delete_remains_stopped_and_does_not_drop_state(tmp_path, monkeypatch):
    repo = Repository(tmp_path)
    folder = repo.create(config())
    sup = Supervisor(repo)
    sup.request_start(folder)
    key = sup._key(folder)
    def fail(*args, **kwargs):
        raise OSError('filesystem refused deletion')
    monkeypatch.setattr(repo, 'delete', fail)
    with pytest.raises(OSError, match='filesystem refused deletion'):
        sup.delete_bot(folder, discard_paper_state=True)
    assert folder.exists()
    assert sup.coordinator.get_registration(key)['desired'] == 'STOPPED'
    assert sup.coordinator.get_registration(key) is not None


def test_registry_cannot_retire_running_bot(tmp_path):
    repo = Repository(tmp_path)
    folder = repo.create(config())
    sup = Supervisor(repo)
    key = sup.register(folder)
    sup.coordinator.set_desired(key, 'RUNNING')
    with pytest.raises(ValueError):
        sup.coordinator.unregister(key)
    assert sup.coordinator.get_registration(key)['desired'] == 'RUNNING'


def test_qt_delete_routes_through_lifecycle_supervisor():
    text = (Path(__file__).parents[1] / 'bcc' / 'ui.py').read_text(encoding='utf-8')
    assert 'self.supervisor.delete_bot(folder, discard_paper_state=True)' in text
    assert 'self.repo.delete(folder, discard_paper_state=True)' not in text
