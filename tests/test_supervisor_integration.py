from pathlib import Path
from bcc.supervisor import Supervisor
from bcc.coordination import Coordinator

class Repo:
    def __init__(self, root):
        self.root = root
        self.bots = root / 'MY_BOTS'
        self.bots.mkdir()
        self.folder = self.bots / 'RSI' / 'one'
        self.folder.mkdir(parents=True)
    def resolve(self, folder):
        resolved = Path(folder).resolve()
        if resolved != self.folder.resolve():
            raise ValueError('missing')
        return resolved
    def list(self):
        return [(self.folder, {'strategy': 'RSI'})]

class Life:
    def __init__(self):
        self.state = 'Остановлен'
        self.calls = []
    def status(self, folder):
        return self.state
    def start(self, folder):
        self.calls.append('start')
        self.state = 'Работает'
    def stop(self, folder, force=False):
        self.calls.append('stop')
        self.state = 'Остановлен'

def test_reconcile_registration_restart_and_durable_stop(tmp_path):
    repo = Repo(tmp_path)
    life = Life()
    supervisor = Supervisor(repo, lifecycle=life)
    assert supervisor.reconcile_all()[0].action == 'none'
    assert supervisor.request_start(repo.folder).action == 'queued'
    assert life.calls == []
    assert supervisor.reconcile_one(repo.folder).action == 'start'
    assert supervisor.reconcile_all()[0].action == 'none'
    assert life.calls == ['start']
    # Desired state survives a new supervisor instance.
    recovered = Supervisor(repo, lifecycle=life)
    assert recovered.reconcile_all()[0].desired == 'RUNNING'
    assert recovered.request_stop(repo.folder).action == 'queued'
    assert life.calls == ['start']
    assert recovered.reconcile_one(repo.folder).action == 'stop'
    assert recovered.reconcile_all()[0].desired == 'STOPPED'
    assert life.calls == ['start', 'stop']

def test_reconcile_reports_failure_and_does_not_invent_success(tmp_path):
    repo = Repo(tmp_path)
    life = Life()
    def fail(_):
        raise RuntimeError('no capacity')
    life.start = fail
    supervisor = Supervisor(repo, lifecycle=life)
    assert supervisor.request_start(repo.folder).action == 'queued'
    result = supervisor.reconcile_one(repo.folder)
    assert result.action == 'error' and 'no capacity' in result.error
    assert result.desired == 'RUNNING'


def test_durable_force_stop_overrides_pending_graceful_shutdown(tmp_path):
    repo = Repo(tmp_path)
    life = Life()
    life.state = 'Остановка'
    def stop(folder, force=False):
        life.calls.append(('stop', force))
        life.state = 'Остановлен'
    life.stop = stop
    ui = Supervisor(repo, lifecycle=life)
    ui.submit(repo.folder, running=False, force=True)
    service = Supervisor(repo, lifecycle=life)
    result = service.reconcile_one(repo.folder)
    assert result.action == 'force-stop'
    assert life.calls == [('stop', True)]
    assert service.coordinator.get_registration(service._key(repo.folder))['force_stop'] is True
    service.reconcile_one(repo.folder)
    assert service.coordinator.get_registration(service._key(repo.folder))['force_stop'] is False

def test_force_stop_intent_is_acknowledged_when_process_already_dead(tmp_path):
    repo = Repo(tmp_path)
    life = Life()
    ui = Supervisor(repo, lifecycle=life)
    ui.submit(repo.folder, running=False, force=True)
    restarted = Supervisor(repo, lifecycle=life)
    result = restarted.reconcile_one(repo.folder)
    assert result.action == 'none'
    assert restarted.coordinator.get_registration(restarted._key(repo.folder))['force_stop'] is False
