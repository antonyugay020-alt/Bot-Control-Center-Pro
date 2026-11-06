"""System boundary: GUI intent is an indivisible durable command, not an OS call."""
import sqlite3
import threading
import pytest
from bcc.coordination import Coordinator
from bcc.supervisor import Supervisor
from test_supervisor_integration import Repo, Life


def test_failed_publication_rolls_back_registration_revision_and_retry_reset(tmp_path):
    c = Coordinator(tmp_path / 'authority.sqlite'); c.initialize()
    c.register('live'); c.set_desired('live', 'RUNNING')
    c.record_restart_failure('live', 'crashed')
    original = c.command_status('live')
    with sqlite3.connect(c.path) as db:
        db.execute("""CREATE TRIGGER deny_command BEFORE INSERT ON command_observations
            BEGIN SELECT RAISE(ABORT,'publication failed'); END""")
        db.execute("""CREATE TRIGGER deny_update BEFORE UPDATE ON command_observations
            BEGIN SELECT RAISE(ABORT,'publication failed'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        c.submit_desired('live', 'STOPPED', force=True, discard_previous_exit=True)
    with pytest.raises(sqlite3.IntegrityError):
        c.submit_desired('new', 'RUNNING')
    assert c.get_registration('new') is None
    assert c.command_status('live') == original
    assert c.restart_status('live')['failures'] == 1


def test_gui_accepts_command_even_when_process_monitoring_is_unavailable(tmp_path):
    repo = Repo(tmp_path)
    life = Life()
    def inaccessible(_):
        raise OSError('process enumeration unavailable')
    life.status = inaccessible
    ui = Supervisor(repo, lifecycle=life)
    accepted = ui.submit(repo.folder, running=True)
    assert accepted.action == 'queued'
    assert ui.coordinator.command_status(accepted.key)['state'] == 'queued'
    ui.submit(repo.folder, running=False, force=True)
    state = ui.coordinator.get_registration(accepted.key)
    assert state['desired'] == 'STOPPED' and state['force_stop']


def test_two_interfaces_publish_complete_versions_without_lost_intents(tmp_path):
    repo = Repo(tmp_path)
    clients = [Supervisor(repo, lifecycle=Life()) for _ in range(2)]
    barrier = threading.Barrier(3)
    results = []
    errors = []
    def submit(client, desired):
        barrier.wait()
        try:
            results.append(client.submit(repo.folder, running=desired))
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=submit,args=(clients[i], bool(i))) for i in range(2)]
    for t in threads: t.start()
    barrier.wait()
    for t in threads: t.join(10)
    assert not errors and len(results) == 2
    key=results[0].key
    status = clients[0].coordinator.command_status(key)
    assert status['revision'] == 2
    assert status['state'] == 'queued'
    assert status['desired'] in ('RUNNING', 'STOPPED')
    assert not clients[0].coordinator.observe_command(key, 1, state='applied',observed='Работает')
    assert clients[1].coordinator.command_status(key)['state'] == 'queued'


def test_drain_admission_is_atomic_on_failed_publication(tmp_path):
    c = Coordinator(tmp_path / 'coord.sqlite'); c.initialize()
    with sqlite3.connect(c.path) as db:
        db.execute("""CREATE TRIGGER deny_drain BEFORE INSERT ON drain_commands
            BEGIN SELECT RAISE(ABORT,'drain publication unavailable'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        c.submit_drain_intent('bot/new', True)
    assert c.get_registration('bot/new') is None


def test_manual_close_admission_keeps_launch_revision_consistent(tmp_path):
    c = Coordinator(tmp_path / 'coord.sqlite'); c.initialize()
    c.submit_desired('bot', 'RUNNING')
    version = c.command_status('bot')['revision']
    assert c.submit_manual_close_intent('bot') == 1
    assert c.pending_manual_close('bot', launch_revision=version) == 1
    c.submit_desired('bot', 'STOPPED')
    assert c.pending_manual_close('bot', launch_revision=version) is None
    with pytest.raises(ValueError):
        c.submit_manual_close_intent('bot')
    c.submit_desired('bot', 'RUNNING')
    assert c.submit_manual_close_intent('bot') == 2
    assert c.pending_manual_close('bot', launch_revision=c.command_status('bot')['revision']) == 2
