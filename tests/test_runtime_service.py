from threading import Event
import pytest
from bcc.runtime_service import HostLease, serve


def test_host_lease_rejects_concurrent_owners(tmp_path):
    path = tmp_path / 'lease'
    with HostLease(path):
        with pytest.raises(RuntimeError, match='already owned'):
            with HostLease(path):
                pass
    with HostLease(path):
        pass


def test_headless_supervision_persists_independent_of_gui(tmp_path):
    done = Event()
    class FakeSupervisor:
        def __init__(self):
            self.cursors = []
            self.inventories = 0
        def inventory(self):
            self.inventories += 1
        def reconcile_batch(self, cursor, *, limit, sync_inventory=True):
            assert sync_inventory is False
            self.cursors.append((cursor, limit))
            if len(self.cursors) == 3:
                done.set()
            return 'key' + str(len(self.cursors)), [len(self.cursors)]
    s = FakeSupervisor()
    observed = []
    serve(tmp_path, stop=done, interval=0.001, batch_size=3,
          supervisor=s, on_result=observed.append)
    assert observed == [1, 2, 3]
    assert s.inventories == 1
    assert s.cursors == [(None, 3), ('key1', 3), ('key2', 3)]


def test_invalid_service_limits(tmp_path):
    for args in ({'interval': 0}, {'batch_size': 0}, {'batch_size': True}):
        with pytest.raises(ValueError):
            serve(tmp_path, **args)
