"""Cross-layer control-plane invariants; old commands never control new launches."""
import sqlite3
import threading

from bcc.coordination import Coordinator
from bcc.supervisor import Supervisor
from test_supervisor_integration import Repo, Life


def test_drain_is_bound_to_current_launch_and_stale_ack_cannot_erase_new_intent(tmp_path):
    c = Coordinator(tmp_path / 'coord.sqlite')
    c.initialize()
    v1 = c.submit_desired('bot', 'RUNNING')
    drain1 = c.submit_drain_intent('bot', True)
    assert c.pending_drain('bot', launch_revision=v1) == (drain1, 1)
    c.submit_desired('bot', 'STOPPED')
    assert c.pending_drain('bot', launch_revision=v1) is None
    v2 = c.submit_desired('bot', 'RUNNING')
    assert c.pending_drain('bot', launch_revision=v2) is None
    drain2 = c.submit_drain_intent('bot', False)
    assert drain2 > drain1
    assert c.pending_drain('bot', launch_revision=v2) == (drain2, 0)
    assert not c.acknowledge_drain('bot', drain1, launch_revision=v1)
    assert c.pending_drain('bot', launch_revision=v2) == (drain2, 0)
    assert c.acknowledge_drain('bot', drain2, launch_revision=v2)
    assert c.pending_drain('bot', launch_revision=v2) is None


def test_migrated_drain_without_epoch_never_replays_in_current_worker(tmp_path):
    path = tmp_path / 'old.sqlite'
    c = Coordinator(path)
    c.initialize()
    c.submit_desired('bot', 'RUNNING')
    # Simulate an installation that already contained pre-epoch drain commands.
    with sqlite3.connect(path) as db:
        db.execute('UPDATE drain_commands SET applied_revision=0,launch_revision=0 WHERE key=?', ('bot',)) if db.execute('SELECT 1 FROM drain_commands WHERE key=?', ('bot',)).fetchone() else db.execute('INSERT INTO drain_commands(key,revision,applied_revision,draining,launch_revision) VALUES(?,1,0,1,0)', ('bot',))
    c.initialize()
    assert c.pending_drain('bot', launch_revision=1) is None


def test_supervisor_does_not_invoke_stale_drain_after_restart(tmp_path):
    repo = Repo(tmp_path)
    life = Life()
    sup = Supervisor(repo, lifecycle=life)
    key = sup.register(repo.folder)
    sup.submit(repo.folder, running=True)
    sup.submit_drain(repo.folder, draining=True)
    sup.submit(repo.folder, running=False)
    sup.submit(repo.folder, running=True)
    result = sup.reconcile_one(repo.folder)
    assert result.action != 'drain'
    assert sup.coordinator.pending_drain(key, launch_revision=sup.coordinator.command_status(key)['revision']) is None


def test_two_clients_cannot_apply_prior_epoch_drain(tmp_path):
    c1 = Coordinator(tmp_path / 'coord.sqlite')
    c1.initialize()
    c2 = Coordinator(c1.path)
    v1 = c1.submit_desired('bot', 'RUNNING')
    old = c1.submit_drain_intent('bot', True)
    c2.submit_desired('bot', 'STOPPED')
    v2 = c2.submit_desired('bot', 'RUNNING')
    new = c2.submit_drain_intent('bot', False)
    assert not c1.acknowledge_drain('bot', old, launch_revision=v1)
    assert c2.pending_drain('bot', launch_revision=v2) == (new, 0)


def test_filesystem_finish_intent_cannot_cross_worker_generation(tmp_path):
    import json
    from bcc.control import finish_requested
    from bcc.storage import atomic_json

    class Account:
        def __init__(self):
            self.state = {'strategy': {}}
            self.writes = 0
        def transaction(self, include_charts=False):
            from contextlib import contextmanager
            @contextmanager
            def tx():
                self.writes += 1
                yield self.state, []
            return tx()

    account = Account()
    atomic_json(tmp_path / 'finish.request',
                {'id': 'older', 'token': 'old-worker', 'draining': True})
    finish_requested(tmp_path, account, expected_token='new-worker')
    assert account.writes == 0
    assert not (tmp_path / 'finish.request').exists()
    atomic_json(tmp_path / 'finish.request',
                {'id': 'current', 'token': 'new-worker', 'draining': True})
    finish_requested(tmp_path, account, expected_token='new-worker')
    assert account.writes == 1
    assert account.state['strategy']['draining'] is True


def test_drain_migration_preserves_legacy_row_and_adds_launch_fence(tmp_path):
    path = tmp_path / 'v1.sqlite'
    c = Coordinator(path)
    c.initialize()
    with sqlite3.connect(path) as db:
        db.execute('DROP TABLE drain_commands')
        db.execute('CREATE TABLE drain_commands(key TEXT PRIMARY KEY,revision INTEGER NOT NULL,applied_revision INTEGER NOT NULL,draining INTEGER NOT NULL)')
        db.execute('INSERT INTO drain_commands VALUES(?,3,2,1)', ('bot',))
    c.initialize()
    with sqlite3.connect(path) as db:
        row = db.execute('SELECT revision,applied_revision,draining,launch_revision FROM drain_commands WHERE key=?', ('bot',)).fetchone()
    assert row == (3,2,1,0)
