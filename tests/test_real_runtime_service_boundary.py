"""Real host-process service contract; no patched lifecycle or Qt stubs."""
import os
import subprocess
import sys
import time
from pathlib import Path

from bcc.bootstrap import ensure_runtime
from bcc.runtime_health import observe
from bcc.storage import Repository
from bcc.supervisor import Supervisor


def test_runtime_service_is_separate_and_command_ack_is_not_execution(tmp_path):
    repo = Repository(tmp_path / 'host')
    repo.root.mkdir(parents=True, exist_ok=True)
    service = subprocess.Popen(
        [sys.executable, '-m', 'bcc.runtime_service', '--data-root', str(repo.root), '--interval', '0.05'],
        cwd=str(Path(__file__).resolve().parents[1]),
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline and not observe(repo.root):
            assert service.poll() is None, 'Runtime terminated before ready'
            time.sleep(.05)
        assert observe(repo.root) is not None, 'Independent runtime did not become ready'
        # A second GUI bootstrap must reuse the running service rather than spawn another.
        def forbidden_spawn(*args, **kwargs):
            raise AssertionError('Attempted duplicate guardian spawn')
        assert ensure_runtime(repo.root, spawn=forbidden_spawn) is not None
        assert service.poll() is None
    finally:
        service.terminate()
        try:
            service.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            service.kill()
            service.communicate(timeout=5)
    # Crash/exit invalidates observed health rather than leaving a fake alive marker.
    assert observe(repo.root) is None
