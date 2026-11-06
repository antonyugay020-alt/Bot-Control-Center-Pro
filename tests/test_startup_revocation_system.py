from pathlib import Path
"""Cross-process lifecycle intent under the start/stop race."""
from bcc.launch_claim import issue_launch, read_claim
from bcc.storage import BotLock, Repository
from bcc.supervisor import Supervisor
from bcc.config import defaults
from bcc.runtime_paths import python_environment


def test_stop_revokes_uncommitted_launch_and_fences_child(tmp_path):
    repo = Repository(tmp_path / 'runtime')
    cfg = defaults(); cfg["name"] = "Startup revocation"
    folder = repo.create(cfg)
    control = Supervisor(repo)
    control.submit(folder, running=True)
    with BotLock(folder):
        issue_launch(folder, 'a'*32)
    # UI changes desired state before startup has claimed its ledger.
    control.submit(folder, running=False, force=True)
    service = Supervisor(repo)
    result = service.reconcile_one(folder)
    assert result.action == 'revoke-startup'
    assert read_claim(folder) is None
    assert service.coordinator.get_registration(service._key(folder))['desired'] == 'STOPPED'
    assert service.reconcile_one(folder).observed == 'Остановлен'


def test_stop_does_not_claim_pending_launch_while_worker_holds_lock(tmp_path):
    class FakeLife:
        def __init__(self): self.calls=[]
        def status(self, folder): return 'Запуск'
        def revoke_pending_launch(self, folder): self.calls.append('revoke'); return False
        def stop(self, folder, *, force=False): raise AssertionError('unverified PID targeted')
    repo = Repository(tmp_path / 'runtime')
    cfg = defaults(); cfg["name"] = "Startup revocation"
    folder = repo.create(cfg)
    life = FakeLife()
    control = Supervisor(repo, lifecycle=life)
    control.submit(folder, running=False, force=True)
    result = control.reconcile_one(folder)
    assert result.action == 'await-startup'
    assert result.observed == 'Запуск'
    assert life.calls == ['revoke']
    assert control.coordinator.get_registration(control._key(folder))['force_stop'] == 1


def test_revoked_token_cannot_start_real_worker(tmp_path):
    """A separate interpreter cannot convert a canceled launch into a ledger writer."""
    import os
    import subprocess
    import sys
    repo = Repository(tmp_path / 'runtime')
    cfg = defaults(); cfg['name'] = 'Revoked real child'
    folder = repo.create(cfg)
    supervisor = Supervisor(repo)
    supervisor.submit(folder, running=True)
    token = 'c' * 32
    with BotLock(folder):
        issue_launch(folder, token)
    supervisor.submit(folder, running=False, force=True)
    assert Supervisor(repo).reconcile_one(folder).action == 'revoke-startup'
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    result = subprocess.run(
        [sys.executable, '-m', 'bcc.worker', '--folder', str(folder), '--token', token],
        env=python_environment(env), capture_output=True, text=True, encoding='utf-8', timeout=15,
    )
    assert result.returncode != 0
    assert not (folder / 'paper.sqlite').exists()
    assert read_claim(folder) is None
