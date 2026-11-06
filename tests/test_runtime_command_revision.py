"""Cross-process intent revision: stale acknowledgement may not eat a new force stop."""
from bcc.coordination import Coordinator
from bcc.supervisor import Supervisor
from test_supervisor_integration import Repo, Life

def test_acknowledge_only_exact_stop_revision(tmp_path):
    c = Coordinator(tmp_path / 'commands.sqlite')
    c.initialize()
    c.register('a')
    c.set_desired('a', 'STOPPED', force=True)
    old = c.get_registration('a')['command_version']
    c.set_desired('a', 'STOPPED', force=True)
    assert not c.clear_force_stop('a', old)
    assert c.get_registration('a')['force_stop']
    current = c.get_registration('a')['command_version']
    assert c.clear_force_stop('a', current)
    assert not c.get_registration('a')['force_stop']

def test_new_start_prevents_stale_stop_ack(tmp_path):
    c = Coordinator(tmp_path / 'commands.sqlite')
    c.initialize()
    c.register('a')
    c.set_desired('a', 'STOPPED', force=True)
    old = c.get_registration('a')['command_version']
    c.set_desired('a', 'RUNNING')
    assert not c.clear_force_stop('a', old)
    assert c.get_registration('a')['desired'] == 'RUNNING'

def test_supervisor_uses_conditional_acknowledgement(tmp_path):
    repo, life = Repo(tmp_path), Life()
    ui = Supervisor(repo, lifecycle=life)
    ui.submit(repo.folder, running=True)
    supervisor = Supervisor(repo, lifecycle=life)
    supervisor.reconcile_all()
    ui.submit(repo.folder, running=False, force=True)
    assert supervisor.reconcile_all()[0].action == 'force-stop'
    assert supervisor.coordinator.get_registration(ui._key(repo.folder))['force_stop']
    assert supervisor.reconcile_all()[0].action == 'none'
    assert not supervisor.coordinator.get_registration(ui._key(repo.folder))['force_stop']

def test_force_stop_survives_delayed_exit_and_service_recreation(tmp_path):
    repo, life = Repo(tmp_path), Life()
    life.state = 'Работает'
    calls = []
    def asynchronous_stop(folder, force=False):
        calls.append(force)
        life.state = 'Остановка'
    life.stop = asynchronous_stop
    ui = Supervisor(repo, lifecycle=life)
    ui.submit(repo.folder, running=False, force=True)
    key = ui._key(repo.folder)
    worker = Supervisor(repo, lifecycle=life)
    assert worker.reconcile_one(repo.folder).action == 'force-stop'
    assert worker.coordinator.get_registration(key)['force_stop'] is True
    restarted = Supervisor(repo, lifecycle=life)
    assert restarted.reconcile_one(repo.folder).action == 'force-stop'
    assert restarted.coordinator.get_registration(key)['force_stop'] is True
    assert calls == [True, True]
    life.state = 'Остановлен'
    assert restarted.reconcile_one(repo.folder).action == 'none'
    assert restarted.coordinator.get_registration(key)['force_stop'] is False
