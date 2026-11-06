"""Exhaustive regression for inline-done callbacks and bounded owner admission."""
from concurrent.futures import Future
from bcc.fleet import Fleet


class ImmediatePool:
    def __init__(self):
        self.completed = 0

    def submit(self, operation):
        future = Future()
        try:
            future.set_result(operation())
        except BaseException as exc:
            future.set_exception(exc)
        self.completed += 1
        return future

    def shutdown(self, **_):
        return None


def test_many_inline_completions_do_not_recurse():
    fleet = Fleet(workers=1)
    fleet._pool.shutdown(wait=True)
    pool = ImmediatePool()
    fleet._pool = pool
    seen = []
    # Queue a large set without auto-dispatch, then drain via a single call.
    with fleet._lock:
        fleet._dispatching = True
        for i in range(2500):
            key = str(i)
            fleet.register(key, lambda i=i: seen.append(i) or i, ephemeral=True)
            assert fleet.request(key)
        fleet._dispatching = False
        fleet._dispatch()
    results = fleet.collect()
    assert pool.completed == 2500
    assert len(results) == 2500
    assert sorted(seen) == list(range(2500))
    assert sorted(value for _, value, error in results if error is None) == list(range(2500))
    assert fleet.snapshot()['registered'] == 0
    fleet.close()
