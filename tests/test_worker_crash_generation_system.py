"""Post-spawn crash recovery must not create an unbounded worker restart storm."""
import sqlite3

from bcc.coordination import Coordinator
from bcc.supervisor import Supervisor
from test_supervisor_integration import Repo, Life


def test_worker_crashes_are_counted_once_per_generation_across_services(tmp_path):
    repo, life = Repo(tmp_path), Life()
    now = [1000.0]
    first = Supervisor(repo, lifecycle=life)
    first.coordinator.clock = lambda: now[0]
    first.request_start(repo.folder)
    key = first._key(repo.folder)
    assert first.reconcile_one(repo.folder).action == 'start'
    assert first.coordinator.restart_status(key)['spawn_epoch'] == 1
    life.state = 'Остановлен'  # first worker died AFTER Popen succeeded
    restarted = Supervisor(repo, lifecycle=life)
    restarted.coordinator.clock = lambda: now[0]
    for _ in range(5):
        assert restarted.reconcile_one(repo.folder).action == 'backoff'
    state = restarted.coordinator.restart_status(key)
    assert state['failures'] == 1
    assert state['handled_exit'] == state['spawn_epoch'] == 1
    assert life.calls == ['start']
    now[0] = state['retry_after'] + 0.01
    assert restarted.reconcile_one(repo.folder).action == 'start'
    assert restarted.coordinator.restart_status(key)['spawn_epoch'] == 2
    life.state = 'Остановлен'
    assert restarted.reconcile_one(repo.folder).action == 'backoff'
    second = restarted.coordinator.restart_status(key)
    assert second['failures'] == 2
    assert second['retry_after'] - now[0] == 4
    assert restarted.reconcile_one(repo.folder).action == 'backoff'
    assert restarted.coordinator.restart_status(key)['failures'] == 2


def test_explicit_start_overrides_crash_backoff_without_losing_new_ownership(tmp_path):
    repo, life = Repo(tmp_path), Life()
    host = Supervisor(repo, lifecycle=life)
    host.request_start(repo.folder)
    host.reconcile_one(repo.folder)
    life.state = 'Остановлен'
    assert host.reconcile_one(repo.folder).action == 'backoff'
    host.request_start(repo.folder)  # explicit operator override
    key = host._key(repo.folder)
    assert host.coordinator.restart_status(key)['failures'] == 0
    assert host.reconcile_one(repo.folder).action == 'start'
    assert host.coordinator.restart_status(key)['spawn_epoch'] == 2
    life.state = 'Остановлен'
    assert host.reconcile_one(repo.folder).action == 'backoff'


def test_long_running_worker_resets_backoff_without_resetting_generation(tmp_path):
    repo, life = Repo(tmp_path), Life()
    now = [1000.0]
    host = Supervisor(repo, lifecycle=life)
    host.coordinator.clock = lambda: now[0]
    host.request_start(repo.folder)
    host.reconcile_one(repo.folder)
    life.state = 'Остановлен'
    assert host.reconcile_one(repo.folder).action == 'backoff'
    now[0] += 3
    assert host.reconcile_one(repo.folder).action == 'start'
    key = host._key(repo.folder)
    assert host.coordinator.restart_status(key)['failures'] == 1
    now[0] += 31
    assert host.reconcile_one(repo.folder).action == 'none'
    assert host.coordinator.restart_status(key)['failures'] == 0
    life.state = 'Остановлен'
    assert host.reconcile_one(repo.folder).action == 'backoff'
    assert host.coordinator.restart_status(key)['failures'] == 1


def test_legacy_restart_policy_upgrades_without_erasing_backoff(tmp_path):
    path = tmp_path / 'legacy.sqlite'
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE restart_policy (
            key TEXT PRIMARY KEY, failures INTEGER NOT NULL DEFAULT 0,
            retry_after REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '')""")
        db.execute("INSERT INTO restart_policy VALUES('existing',3,1000,'previous crash')")
    c = Coordinator(path)
    c.initialize()
    status = c.restart_status('existing')
    assert status['failures'] == 3
    assert status['retry_after'] == 1000
    assert status['spawn_epoch'] == status['handled_exit'] == 0
