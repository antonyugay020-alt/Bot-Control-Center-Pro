"""Resource-bounded dynamic fleet orchestration, independent of bot count.

Fleet keys identify state owners, not worker slots. The scheduler admits a
configurable number of concurrent executions, with at most one per owner.
All account mutations remain the responsibility of their store transaction.
"""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from threading import RLock


class FleetClosed(RuntimeError):
    pass


class Fleet:
    """Per-owner serial execution with bounded, priority-aware admission.

    Registration is independent of concurrency. A priority request can displace
    an *unstarted* ordinary follow-up, never a running operation. Duplicated
    owners coalesce; promotion never creates another execution of that owner.
    For short-lived service work, ephemeral registrations retire on completion.
    """
    def __init__(self, workers=8, *, max_pending=None):
        if type(workers) is not int or workers < 1:
            raise ValueError('workers must be a positive integer')
        if max_pending is not None and (type(max_pending) is not int or max_pending < 1):
            raise ValueError('max_pending must be a positive integer')
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='bcc-fleet')
        self._workers = workers
        self._max_pending = max_pending
        self._lock = RLock()
        self._registered = {}
        self._ephemeral = set()
        self._waiting = deque()
        self._urgent = deque()
        self._queued = set()
        self._active = {}
        self._results = deque()
        self._closed = False
        # A completed future may run its callback inline while dispatch holds
        # the lock. Never recursively enter the dispatch loop.
        self._dispatching = False

    def register(self, key, operation, *, ephemeral=False):
        if not isinstance(key, str) or not key or not callable(operation):
            raise ValueError('nonempty owner key and callable required')
        with self._lock:
            if self._closed:
                raise FleetClosed('fleet closed')
            if key in self._registered or key in self._active:
                raise ValueError('owner already registered or still draining')
            self._registered[key] = operation
            if ephemeral:
                self._ephemeral.add(key)

    def ensure_registered(self, key, operation, *, ephemeral=False):
        """Atomically reuse an existing owner or register one new operation.

        The caller may offer refreshed closures on each registry sweep; only
        the first submitted operation owns the generation until it drains.
        """
        if not isinstance(key, str) or not key or not callable(operation):
            raise ValueError('nonempty owner key and callable required')
        with self._lock:
            if self._closed:
                raise FleetClosed('fleet closed')
            if key not in self._registered:
                self._registered[key] = operation
                if ephemeral:
                    self._ephemeral.add(key)

    def request(self, key, *, urgent=False):
        """Return whether work was accepted. Only pending ordinary work is evictable."""
        with self._lock:
            if self._closed:
                raise FleetClosed('fleet closed')
            if key not in self._registered:
                raise KeyError(key)
            if key in self._queued:
                if urgent and key in self._waiting:
                    self._waiting.remove(key)
                    self._urgent.append(key)
                self._dispatch()
                return True
            if self._max_pending is not None and len(self._queued) >= self._max_pending:
                if not urgent or not self._waiting:
                    self._retire_if_idle(key)
                    return False
                displaced = self._waiting.pop()
                self._queued.discard(displaced)
                self._retire_if_idle(displaced)
            (self._urgent if urgent else self._waiting).append(key)
            self._queued.add(key)
            self._dispatch()
            return True

    def _retire_if_idle(self, key):
        if key in self._ephemeral and key not in self._active and key not in self._queued:
            self._registered.pop(key, None)
            self._ephemeral.discard(key)

    def _next_runnable(self):
        """Choose the highest-priority *runnable* owner.

        A follow-up for an already running owner must not occupy an idle
        worker slot or prevent an unrelated owner from progressing.  This
        selection is bounded by the number of queued owners and preserves
        FIFO ordering within each lane.
        """
        for lane in (self._urgent, self._waiting):
            for _ in range(len(lane)):
                key = lane.popleft()
                if key not in self._registered:
                    self._queued.discard(key)
                    continue
                if key in self._active:
                    lane.append(key)
                    continue
                self._queued.remove(key)
                return key
        return None

    def _dispatch(self):
        if self._dispatching:
            return
        self._dispatching = True
        try:
            while not self._closed and len(self._active) < self._workers and self._queued:
                key = self._next_runnable()
                if key is None:
                    break
                future = self._pool.submit(self._registered[key])
                self._active[key] = future
                # A completed Future invokes callbacks synchronously. The
                # reentry guard lets this outer loop drain additional work.
                future.add_done_callback(lambda done, owner=key: self._complete(owner, done))
        finally:
            self._dispatching = False

    def _complete(self, key, future):
        try:
            outcome = (key, future.result(), None)
        except BaseException as exc:
            outcome = (key, None, exc)
        with self._lock:
            self._results.append(outcome)
            self._active.pop(key, None)
            self._retire_if_idle(key)
            if not self._closed:
                self._dispatch()

    def unregister(self, key):
        with self._lock:
            self._registered.pop(key, None)
            self._ephemeral.discard(key)
            self._waiting = deque(k for k in self._waiting if k != key)
            self._urgent = deque(k for k in self._urgent if k != key)
            self._queued.discard(key)

    def collect(self):
        with self._lock:
            results = list(self._results)
            self._results.clear()
            return results

    def snapshot(self):
        with self._lock:
            return {'registered': len(self._registered), 'active': len(self._active),
                    'waiting': len(self._queued), 'urgent_waiting': len(self._urgent),
                    'capacity': self._workers, 'closed': self._closed}

    def close(self, wait=True):
        with self._lock:
            self._closed = True
            self._registered.clear()
            self._ephemeral.clear()
            self._waiting.clear()
            self._urgent.clear()
            self._queued.clear()
        self._pool.shutdown(wait=wait, cancel_futures=not wait)
        return self.collect()
