"""Real OS-process acceptance for the control plane readiness contract."""
import os
import subprocess
import sys
import time

import pytest

from bcc.runtime_process import owned_service, stop_owned_service
from bcc.runtime_health import observe, wait_ready, HEALTH_FILENAME
from bcc.runtime_paths import python_environment


def test_runtime_health_requires_real_process_and_retires_after_exit(tmp_path):
    root = tmp_path / 'state'
    root.mkdir()
    env = {**os.environ, 'PYTHONPATH': str(__import__('pathlib').Path(__file__).resolve().parents[1])}
    process = subprocess.Popen(
        [sys.executable, '-m', 'bcc.runtime_service', '--data-root', str(root),
         '--interval', '0.05'], env=python_environment(env), stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, text=True, encoding='utf-8',
    )
    try:
        health = wait_ready(root, timeout=10, poll=0.05)
        actual = owned_service(process, root, health)
        assert actual is not None
        assert actual.pid == health['pid']
        assert observe(root)['run_id'] == health['run_id']
        assert process.poll() is None
        # PID and creation time are not sufficient if the source process goes away.
    finally:
        stop_owned_service(process, root)
    assert observe(root) is None


def test_invalid_stale_health_never_authorizes_launch(tmp_path):
    (tmp_path / HEALTH_FILENAME).write_text('{"state":"ready","pid":1}', encoding='utf-8')
    assert observe(tmp_path) is None
    with pytest.raises(RuntimeError, match='did not become ready'):
        wait_ready(tmp_path, timeout=0.015, poll=0.005)


def test_guardian_recovers_service_and_changes_verified_generation(tmp_path):
    """Actual guardian/service OS-process recovery, not a mocked lifecycle."""
    from bcc import process_os as psutil
    from pathlib import Path
    root = tmp_path / 'recovery-state'
    root.mkdir()
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1])}
    guard = subprocess.Popen(
        [sys.executable, '-m', 'bcc.service_guard', '--data-root', str(root)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        first = wait_ready(root, timeout=12, poll=0.05)
        assert first['pid'] != guard.pid
        psutil.Process(first['pid']).kill()
        end = time.monotonic() + 12
        recovered = None
        while time.monotonic() < end:
            candidate = observe(root)
            if candidate and candidate['run_id'] != first['run_id']:
                recovered = candidate
                break
            time.sleep(0.05)
        assert recovered is not None, 'guardian failed to restore authoritative service'
        assert recovered['pid'] != first['pid']
    finally:
        guard.terminate()
        try:
            guard.wait(timeout=6)
        except subprocess.TimeoutExpired:
            guard.kill()
            guard.wait(timeout=6)
        # Clean up any child left by an interrupted test.
        health = observe(root)
        if health:
            psutil.Process(health['pid']).terminate()
