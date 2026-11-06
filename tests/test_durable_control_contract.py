from bcc.supervisor import Supervisor
from test_supervisor_integration import Repo, Life


def test_ui_intent_never_executes_worker_and_service_owns_transition(tmp_path):
    repo, life = Repo(tmp_path), Life()
    ui = Supervisor(repo, lifecycle=life)
    requested = ui.submit(repo.folder, running=True)
    assert requested.action == 'queued'
    assert life.calls == []
    service = Supervisor(repo, lifecycle=life)
    assert service.reconcile_all()[0].action == 'start'
    assert life.calls == ['start']
    ui.submit(repo.folder, running=False, force=True)
    assert life.calls == ['start']
    assert service.coordinator.snapshot()[0]['force_stop'] is True
    assert service.reconcile_all()[0].action == 'force-stop'
    assert life.calls == ['start','stop']
    # A successful signal is not proof that the process has exited.
    assert service.coordinator.snapshot()[0]['force_stop'] is True
    assert service.reconcile_all()[0].action == 'none'
    assert service.coordinator.snapshot()[0]['force_stop'] is False


def test_restart_keeps_desired_intent_without_gui(tmp_path):
    repo, life = Repo(tmp_path), Life()
    Supervisor(repo, lifecycle=life).submit(repo.folder, running=True)
    Supervisor(repo, lifecycle=life).reconcile_all()
    life.state = 'Остановлен'
    resumed = Supervisor(repo, lifecycle=life)
    assert resumed.reconcile_all()[0].action == 'backoff'
    delay = resumed.coordinator.restart_status(resumed._key(repo.folder))['retry_after']
    resumed.coordinator.clock = lambda: delay + 1
    assert resumed.reconcile_all()[0].action == 'start'
