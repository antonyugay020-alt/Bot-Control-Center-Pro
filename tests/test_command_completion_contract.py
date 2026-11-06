"""End-to-end command protocol: intent is distinct from observed execution."""
from bcc.supervisor import Supervisor
from test_supervisor_integration import Repo, Life


def test_durable_command_ack_after_actual_process_transition(tmp_path):
    repo, life = Repo(tmp_path), Life()
    gui = Supervisor(repo, lifecycle=life)
    gui.request_start(repo.folder)
    key = gui._key(repo.folder)
    assert gui.coordinator.command_status(key)['state'] == 'queued'
    service = Supervisor(repo, lifecycle=life)
    result = service.reconcile_one(repo.folder)
    assert result.action == 'start'
    status = gui.coordinator.command_status(key)
    assert status['desired'] == 'RUNNING' and status['state'] == 'applied'
    assert status['observed'] == 'Работает'
    gui.request_stop(repo.folder)
    assert service.coordinator.command_status(key)['state'] == 'queued'
    assert service.reconcile_one(repo.folder).action == 'stop'
    assert gui.coordinator.command_status(key)['state'] == 'applied'


def test_stale_observation_cannot_ack_newer_start(tmp_path):
    repo, life = Repo(tmp_path), Life()
    sup = Supervisor(repo, lifecycle=life)
    key = sup.register(repo.folder)
    sup.request_start(repo.folder)
    old = sup.coordinator.command_status(key)['revision']
    sup.request_stop(repo.folder)
    assert not sup.coordinator.observe_command(key, old, state='applied', observed='Работает')
    assert sup.coordinator.command_status(key)['state'] == 'queued'
    sup.request_start(repo.folder)
    new = sup.coordinator.command_status(key)['revision']
    assert new > old
    assert not sup.coordinator.observe_command(key, old, state='applied')
    assert sup.coordinator.command_status(key)['state'] == 'queued'


def test_command_error_persists_across_service_restart(tmp_path):
    repo, life = Repo(tmp_path), Life()
    sup = Supervisor(repo, lifecycle=life)
    sup.request_start(repo.folder)
    def fail(folder):
        raise RuntimeError('worker missing')
    life.start = fail
    assert sup.reconcile_one(repo.folder).action == 'error'
    reopened = Supervisor(repo, lifecycle=life)
    status = reopened.coordinator.command_status(reopened._key(repo.folder))
    assert status['state'] == 'error'
    assert 'worker missing' in status['error']


def test_only_observed_exit_confirms_force_stop(tmp_path):
    repo, life = Repo(tmp_path), Life()
    life.state = 'Работает'
    def slow_stop(folder, force=False):
        life.state = 'Остановка'
    life.stop = slow_stop
    sup = Supervisor(repo, lifecycle=life)
    sup.request_stop(repo.folder, force=True)
    key = sup._key(repo.folder)
    assert sup.reconcile_one(repo.folder).action == 'force-stop'
    assert sup.coordinator.command_status(key)['state'] == 'running'
    assert sup.coordinator.get_registration(key)['force_stop']
    life.state = 'Остановлен'
    sup.reconcile_one(repo.folder)
    assert sup.coordinator.command_status(key)['state'] == 'applied'
