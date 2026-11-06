import threading
import pytest
from bcc.service_guard import supervise


class SimulatedProcess:
    def __init__(self, code):
        self.code = code
    def poll(self):
        return self.code
    def wait(self, timeout=None):
        return self.code


def test_recover_after_repeated_service_crashes(tmp_path):
    stop = threading.Event()
    calls = []
    def spawn(*args, **kwargs):
        calls.append(args)
        return SimulatedProcess(7)
    def on_exit(_code):
        if len(calls) == 3:
            stop.set()
    supervise(tmp_path, stop=stop, spawn=spawn, base_delay=0.001,
              max_delay=0.002, on_exit=on_exit)
    assert len(calls) == 3
    assert all('bcc.runtime_service' in command for (command,) in calls)


def test_guardian_has_exclusive_lease(tmp_path):
    from bcc.runtime_service import HostLease
    with HostLease(tmp_path / '.runtime_guard.lock'):
        with pytest.raises(RuntimeError, match='already owned'):
            supervise(tmp_path, stop=threading.Event(), base_delay=0.001)


def test_bad_policy_rejected(tmp_path):
    with pytest.raises(ValueError):
        supervise(tmp_path, base_delay=0)
