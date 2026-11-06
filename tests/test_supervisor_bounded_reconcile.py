from pathlib import Path
from bcc.supervisor import Supervisor
from test_supervisor_integration import Repo, Life


def test_bounded_reconciliation_recovers_process_after_shell_restart(tmp_path):
    repo = Repo(tmp_path)
    life = Life()
    first = Supervisor(repo, lifecycle=life)
    assert first.request_start(repo.folder).action == 'queued'
    assert first.reconcile_one(repo.folder).action == 'start'
    life.state = 'Остановлен'  # worker crashed after first shell was closed
    restarted = Supervisor(repo, lifecycle=life)
    cursor, results = restarted.reconcile_batch(limit=1)
    assert len(results) == 1
    # An observed post-launch crash is not an immediate restart: the host
    # applies durable backoff even after a GUI/service recreation.
    assert results[0].action == 'backoff'
    assert life.calls == ['start']
    restarted.coordinator.clock = lambda: restarted.coordinator.restart_status(
        restarted._key(repo.folder))['retry_after'] + 1
    _, results = restarted.reconcile_batch(limit=1)
    assert results[0].action == 'start'
    assert life.calls == ['start', 'start']
    assert cursor == restarted._key(repo.folder)


def test_batch_round_robin_is_bounded_and_fair(tmp_path):
    class ManyRepo:
        def __init__(self, root):
            self.root, self.bots = root, root / 'MY_BOTS'
            self.bots.mkdir()
            self.folders = []
            for i in range(7):
                p = self.bots / ('bot%02d' % i)
                p.mkdir()
                self.folders.append(p)
        def resolve(self, folder):
            p = Path(folder).resolve()
            if p not in self.folders:
                raise ValueError('missing')
            return p
        def list(self):
            return [(x, {'strategy': 'VELES'}) for x in self.folders]
    repo = ManyRepo(tmp_path)
    s = Supervisor(repo, lifecycle=Life())
    cursor = None
    seen = []
    for _ in range(7):
        cursor, results = s.reconcile_batch(cursor, limit=1)
        assert len(results) == 1
        seen.append(results[0].key)
    assert len(set(seen)) == 7
    cursor, next_results = s.reconcile_batch(cursor, limit=1)
    assert next_results[0].key == seen[0]


def test_batch_rejects_invalid_limits(tmp_path):
    s = Supervisor(Repo(tmp_path), lifecycle=Life())
    import pytest
    for v in (0, -1, 0.5, True):
        with pytest.raises(ValueError):
            s.reconcile_batch(limit=v)
