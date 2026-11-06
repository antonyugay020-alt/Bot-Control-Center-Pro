import threading
import time
import unittest
from bcc.fleet import Fleet


class FleetArchitectureTests(unittest.TestCase):
    def test_dynamic_membership_coalescing_and_drain(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []
        def work():
            calls.append('start')
            entered.set()
            release.wait(2)
            return len(calls)
        fleet = Fleet(workers=1)
        try:
            fleet.register('account:a', work)
            fleet.request('account:a')
            self.assertTrue(entered.wait(2))
            for _ in range(100):
                fleet.request('account:a')
            self.assertEqual(fleet.snapshot()['waiting'], 1)
            fleet.unregister('account:a')
            release.set()
            for _ in range(100):
                if fleet.collect():
                    break
                time.sleep(.005)
            self.assertEqual(len(calls), 1)
        finally:
            release.set()
            fleet.close()

    def test_fairness_independent_of_fleet_size(self):
        with _managed(Fleet(workers=3)) as fleet:
            for i in range(257):
                fleet.register(str(i), lambda i=i: i)
                fleet.request(str(i))
            assert fleet.snapshot()['registered'] == 257
            seen = set()
            until = time.monotonic() + 5
            while time.monotonic() < until and len(seen) < 257:
                seen.update(key for key, _, err in fleet.collect() if err is None)
                time.sleep(.002)
            self.assertEqual(len(seen), 257)

class _managed:
    def __init__(self, obj): self.obj = obj
    def __enter__(self): return self.obj
    def __exit__(self, *_): self.obj.close()
