"""Per-owner isolated strategy execution with resource-configured concurrency.

Owner registration has no artificial numeric ceiling. Optional caller-selected
queue admission is distinct from the number of bots in the fleet.
"""
from concurrent.futures import ThreadPoolExecutor
from threading import Lock


class DispatchBusy(RuntimeError):
    pass


class StrategyDispatcher:
    """Isolate owners and failures while permitting arbitrary fleet membership.

    Worker concurrency is an adjustable thread budget, not a maximum bot count.
    An unset queue budget imposes no unrelated fixed ceiling; callers may
    explicitly enforce workload backpressure when needed.
    """

    def __init__(self, workers=8, maximum_pending=None, thread_name_prefix="bot-exec"):
        if not isinstance(workers, int) or isinstance(workers, bool) or workers < 1:
            raise ValueError('workers must be positive')
        if maximum_pending is not None and (type(maximum_pending) is not int or maximum_pending < workers):
            raise ValueError('maximum_pending must be None or at least workers')
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=thread_name_prefix)
        self._pending = {}
        self._limit = maximum_pending
        self._lock = Lock()
        self._closed = False

    def submit(self, key, function, *args, **kwargs):
        if not isinstance(key, str) or not key:
            raise ValueError('bot key must be a nonempty string')
        with self._lock:
            if self._closed:
                raise DispatchBusy('dispatcher is closed')
            if key in self._pending:
                raise DispatchBusy('bot already has outstanding execution')
            if self._limit is not None and len(self._pending) >= self._limit:
                raise DispatchBusy('strategy queue capacity reached')
            future = self._pool.submit(function, *args, **kwargs)
            self._pending[key] = future
            return future

    def collect(self):
        """Return terminal results, including errors, without losing failures."""
        with self._lock:
            done = [(key, future) for key, future in self._pending.items() if future.done()]
            for key, future in done:
                del self._pending[key]
        result = []
        for key, future in done:
            try:
                result.append((key, future.result(), None))
            except Exception as exc:
                result.append((key, None, exc))
        return result

    def close(self, wait=True):
        with self._lock:
            self._closed = True
        self._pool.shutdown(wait=wait, cancel_futures=not wait)
        return self.collect()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
