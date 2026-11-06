"""Cross-process Hub spawn guard: bounded wait and no leaked file handles."""

import errno
import os
import threading
import time

import pytest

from bcc.hub_supervisor import HubUnavailable, _locked


def test_spawn_lock_cannot_block_dashboard_forever(tmp_path):
    path = tmp_path / "spawn.lock"
    observed = []
    with _locked(path, timeout=1):
        def contender():
            begin = time.monotonic()
            try:
                with _locked(path, timeout=.15):
                    observed.append("incorrectly_acquired")
            except HubUnavailable:
                observed.append(time.monotonic() - begin)
        runner = threading.Thread(target=contender)
        runner.start()
        runner.join(timeout=2)
        assert not runner.is_alive(), "spawn lock waiter did not finish"
    assert len(observed) == 1 and isinstance(observed[0], float)
    assert .1 <= observed[0] < 1.0
    # A timed-out contender must not poison later starts.
    with _locked(path, timeout=.5):
        assert path.is_file()


def test_spawn_lock_closes_file_after_unexpected_lock_error(tmp_path, monkeypatch):
    import builtins
    # Patch the actual platform lock primitive; fcntl does not exist on Windows.
    if os.name == "nt":
        import msvcrt as lock_module
        method = "locking"
    else:
        import fcntl as lock_module
        method = "flock"

    path = tmp_path / "spawn.lock"
    opened = []
    real_open = builtins.open
    real_flock = getattr(lock_module, method)

    def capture_open(name, *args, **kwargs):
        stream = real_open(name, *args, **kwargs)
        if str(name) == str(path):
            opened.append(stream)
        return stream

    def fail(*args):
        raise OSError(errno.EIO, "unexpected lock I/O failure")

    monkeypatch.setattr(builtins, "open", capture_open)
    monkeypatch.setattr(lock_module, method, fail)
    with pytest.raises(OSError, match="unexpected lock"):
        with _locked(path, timeout=.5):
            pass
    assert len(opened) == 1 and opened[0].closed
    monkeypatch.setattr(lock_module, method, real_flock)
    with _locked(path, timeout=.5):
        pass


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), None, "8", True])
def test_spawn_lock_rejects_invalid_timeout_without_creating_file(tmp_path, bad):
    path = tmp_path / "spawn.lock"
    with pytest.raises(ValueError):
        _locked(path, timeout=bad)
    assert not path.exists()


def test_hub_spawn_record_failure_does_not_leave_orphan(tmp_path, monkeypatch):
    """If metadata persistence fails, the newly created service must be reaped."""
    import subprocess
    import sys
    import bcc.hub_supervisor as supervisor

    launched = []
    def fake_spawn(_args, **kwargs):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            **kwargs,
        )
        launched.append(proc)
        return proc

    def fail_atomic(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "simulated storage full")

    monkeypatch.setattr(supervisor, "_spawn_hub", fake_spawn)
    monkeypatch.setattr(supervisor, "atomic_json", fail_atomic)
    try:
        with pytest.raises(OSError, match="simulated storage full"):
            supervisor.ensure_hub(tmp_path, timeout=1)
        assert len(launched) == 1
        assert launched[0].poll() is not None, "failed Hub spawn leaked a live process"
        assert not (tmp_path / "market_hub_process.json").exists()
    finally:
        for proc in launched:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
