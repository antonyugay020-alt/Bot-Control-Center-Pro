from bcc.supervisor import Supervisor
from test_supervisor_integration import Repo, Life


def test_failure_backoff_survives_shell_restart_and_user_override(tmp_path):
    repo = Repo(tmp_path)
    life = Life()
    attempts = []
    def failed_start(folder):
        attempts.append(folder)
        raise RuntimeError('crash')
    life.start = failed_start
    first = Supervisor(repo, lifecycle=life)
    assert first.request_start(repo.folder).action == 'queued'
    result = first.reconcile_one(repo.folder)
    assert result.action == 'error' and len(attempts) == 1
    second = Supervisor(repo, lifecycle=life)
    assert second.reconcile_all()[0].action == 'backoff'
    assert len(attempts) == 1
    assert second.request_start(repo.folder).action == 'queued'
    assert second.reconcile_one(repo.folder).action == 'error'
    assert len(attempts) == 2
    assert second.request_stop(repo.folder).action == 'queued'
    assert second.reconcile_one(repo.folder).action == 'none'
    assert second.coordinator.restart_status(second._key(repo.folder))['failures'] == 0


def test_supervisor_retries_only_after_delay(tmp_path):
    repo = Repo(tmp_path)
    life = Life()
    now = [1000.0]
    sup = Supervisor(repo, lifecycle=life)
    sup.coordinator.clock = lambda: now[0]
    calls = []
    def fail(folder):
        calls.append(1)
        raise RuntimeError('offline')
    life.start = fail
    assert sup.request_start(repo.folder).action == 'queued'
    assert sup.reconcile_one(repo.folder).action == 'error'
    assert sup.reconcile_all()[0].action == 'backoff'
    now[0] += 3
    assert sup.reconcile_all()[0].action == 'error'
    assert len(calls) == 2
    assert sup.coordinator.restart_status(sup._key(repo.folder))['failures'] == 2
