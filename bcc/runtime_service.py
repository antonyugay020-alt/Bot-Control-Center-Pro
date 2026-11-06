"""UI-independent desired-state reconciler for the Windows bot runtime.

Only one service process may own the host reconciliation lease. Individual bot
identity/financial fences remain enforced in Lifecycle and Store. The GUI may
submit desired-state commands without becoming the heartbeat owner.
"""
import argparse
import os
import signal
import time
import logging
from pathlib import Path
from threading import Event, Thread

from .storage import Repository
from .supervisor import Supervisor
from .runtime_health import new_run_id, publish
from .fleet import Fleet
from .supervisor import ReconcileResult


class HostLease:
    """Cross-process, crash-released, non-blocking host lock."""
    def __init__(self, path):
        self.path = Path(path)
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                if self.path.stat().st_size == 0:
                    self.handle.write(b'0')
                    self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.handle.close()
            self.handle = None
            raise RuntimeError('Runtime supervision is already owned') from error
        return self

    def __exit__(self, *_):
        if self.handle is not None:
            try:
                if os.name == 'nt':
                    import msvcrt
                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            finally:
                self.handle.close()
                self.handle = None


def advance_cursor(cursor, keys, accepted):
    """Commit only the admitted registry prefix, wrap after exhaustion."""
    if accepted < 0 or accepted > len(keys):
        raise ValueError('invalid admission count')
    return keys[accepted - 1] if accepted else (None if not keys else cursor)


class ReconciliationPlane:
    """Bounded admission for durable bot processes, never an in-process trader.

    Each owner can have one active reconciliation and one coalesced follow-up.
    A shared Fleet serves urgent and ordinary intents without allowing
    concurrent mutations to the same owner. The worker OS processes and their
    ledger ownership are independent from this short-lived task pool.
    """
    def __init__(self, supervisor, *, workers=8):
        self.supervisor = supervisor
        # Resource budget lives in the executor itself. Reconciliation keys
        # are transient and never retained after their outstanding work drains.
        self.fleet = Fleet(workers=workers, max_pending=workers)

    def schedule(self, keys, *, urgent=False):
        """Admit a contiguous prefix and return its size.

        The caller must not advance its durable-registry scan cursor past an
        owner that could not be admitted. This is admission control rather
        than best-effort notification: queue overload is never interpreted as
        completed reconciliation. Urgent work can evict pending ordinary work;
        evicted work remains discoverable on the next registry sweep.
        """
        admitted = 0
        for key in keys:
            self.fleet.ensure_registered(
                key, lambda owner=key: self.supervisor.reconcile_key(owner),
                ephemeral=True)
            if not self.fleet.request(key, urgent=urgent):
                break
            admitted += 1
        return admitted

    def collect(self):
        results = []
        for key, value, error in self.fleet.collect():
            if error is not None:
                logging.error('Reconciliation failed for %s: %s', key, error)
                # An executor failure must not disappear from observable
                # results: otherwise the host could publish ready while
                # operators never learn that a requested transition failed.
                results.append(ReconcileResult(key, 'UNKNOWN', 'Ошибка',
                                               'error', f'{type(error).__name__}: {error}'))
                continue
            results.append(value)
        return results

    def close(self):
        return self.fleet.close(wait=True)



class InventoryPlane:
    """One-at-a-time discovery isolated from latency-critical reconciliation.

    A filesystem may stall indefinitely. The runtime must still honor urgent
    stops and retire its host lease on shutdown. Discovery's only durable
    mutation is idempotent registration; it never launches workers or writes
    PAPER accounts. This allows a daemon discovery task to be abandoned on
    service termination without violating trading ownership.
    """
    def __init__(self, supervisor, *, batch_size):
        self.supervisor = supervisor
        self.batch_size = batch_size
        self._thread = None
        self._result = None
        self._error = None

    def _execute(self):
        try:
            self._result = self.supervisor.inventory_slice(limit=self.batch_size)
        except BaseException as exc:
            self._error = exc

    def step(self):
        """Poll one discovery slice without waiting for filesystem I/O."""
        if self._thread is None:
            self._result, self._error = None, None
            self._thread = Thread(target=self._execute, daemon=True,
                                  name='bcc-inventory')
            self._thread.start()
            return False, False
        if self._thread.is_alive():
            return False, False
        self._thread.join(timeout=0)
        self._thread = None
        if self._error is not None:
            raise self._error
        _, completed = self._result
        return True, completed

    def close(self):
        # Do not hold the exclusive service lease waiting on a filesystem
        # which may have stopped responding. A daemon discovery thread does
        # not own process lifecycle or financial accounting state.
        if self._thread is not None:
            self._thread.join(timeout=0.2)


def serve(root, *, stop=None, interval=0.5, batch_size=8, inventory_interval=15, supervisor=None, on_result=None):
    if interval <= 0 or inventory_interval <= 0 or batch_size < 1 or type(batch_size) is not int:
        raise ValueError('invalid service rate')
    root = Path(root).resolve()
    stop = stop or Event()
    with HostLease(root / '.runtime_supervisor.lock'):
        worker = supervisor or Supervisor(Repository(root))
        run_id = new_run_id()
        # Compatibility adapters remain synchronous; the production Supervisor
        # uses the shared bounded execution plane for both priority lanes.
        production = isinstance(worker, Supervisor)
        plane = ReconciliationPlane(worker, workers=batch_size) if production else None
        inventory_plane = InventoryPlane(worker, batch_size=batch_size) if production else None
        try:
            _serve_loop(root, worker, stop, interval, batch_size, inventory_interval,
                        on_result, run_id, plane, inventory_plane)
        finally:
            if plane is not None:
                plane.close()
            if inventory_plane is not None:
                inventory_plane.close()
            (root / '.runtime_health.json').unlink(missing_ok=True)


def _serve_loop(root, worker, stop, interval, batch_size, inventory_interval,
                on_result, run_id, plane, inventory_plane=None):
        cursor = None
        urgent_cursor = None
        next_inventory = 0.0
        inventory_active = False
        failures = 0
        ready_seen = False
        inventory_verified = False
        inventory_fault = False
        while not stop.is_set():
            try:
                now = time.monotonic()
                if now >= next_inventory and not inventory_active:
                    inventory_active = True
                if inventory_active:
                    # Discovery runs in bounded slices alongside reconciliation.
                    # A fleet-sized directory traversal must never monopolize
                    # the service while shutdown commands are pending.
                    try:
                        if inventory_plane is not None:
                            # Do not hold the main control lane behind slow
                            # network folders or SQLite inventory writes.
                            observed, completed = inventory_plane.step()
                            if not observed:
                                completed = False
                        else:
                            sliced = getattr(worker, 'inventory_slice', None)
                            if sliced:
                                _, completed = sliced(limit=batch_size)
                            else:
                                worker.inventory()  # compatibility injection
                                completed = True
                    except Exception:
                        # A failed discovery cannot coexist with a healthy host
                        # claim: registrations may not have been discovered.
                        inventory_fault = True
                        inventory_verified = False
                        ready_seen = False
                        logging.exception('Bot registry inventory failed')
                        inventory_active = False
                        next_inventory = now + interval
                    else:
                        if completed:
                            inventory_fault = False
                            inventory_verified = True
                            inventory_active = False
                            next_inventory = now + inventory_interval
                # Shutdown commands have their own bounded priority lane;
                # they never wait for a complete round-robin registry pass.
                # Compatibility with injected supervisors is intentional.
                if plane is None:
                    urgent = getattr(worker, 'reconcile_urgent', None)
                    if urgent:
                        urgent_cursor, urgent_results = urgent(
                            urgent_cursor, limit=max(1, batch_size // 2))
                    else:
                        urgent_results = []
                    cursor, results = worker.reconcile_batch(
                        cursor, limit=batch_size, sync_inventory=False)
                else:
                    urgent_keys = worker.coordinator.urgent_force_stops(
                        urgent_cursor, limit=max(1, batch_size // 2))
                    # Only advance a cursor through successfully admitted
                    # work. A saturated queue must not silently skip owners.
                    urgent_admitted = plane.schedule(urgent_keys, urgent=True)
                    urgent_cursor = advance_cursor(urgent_cursor, urgent_keys, urgent_admitted)
                    keys = worker.coordinator.next_keys(cursor, limit=batch_size)
                    admitted = plane.schedule(keys)
                    cursor = advance_cursor(cursor, keys, admitted)
                    urgent_results = []
                    results = plane.collect()
                failures = 0
                # The host is not ready just because it could enqueue work.
                # Observe an actual reconciliation result first, or finish
                # inventory on an empty registry. Thereafter heartbeat while
                # awaiting bounded tasks so the watchdog sees a live service.
                if plane is None:
                    ready_seen = True  # injected compatibility service
                elif (any(getattr(item, 'action', 'error') not in ('error', 'orphan')
                          for item in [*results, *urgent_results])
                      or (inventory_verified and
                          not worker.coordinator.has_registrations())):
                    # Only an observed successful reconciliation, or a truly
                    # empty durable registry, can establish readiness.  A
                    # drained Fleet queue is NOT proof that a bot succeeded.
                    ready_seen = True
                # A worker-level error is evidence that this round did not
                # reconcile successfully, even if an older round was healthy.
                # Do not retain a sticky ready flag across failed work.
                had_error = any(getattr(item, 'action', None) == 'error'
                                for item in [*results, *urgent_results])
                if had_error:
                    ready_seen = False
                if inventory_fault:
                    ready_seen = False
                if ready_seen:
                    publish(root, run_id=run_id, state='ready')
                elif had_error or inventory_fault:
                    publish(root, run_id=run_id, state='degraded')
                if on_result:
                    for result in [*urgent_results, *results]:
                        try:
                            on_result(result)
                        except Exception:
                            logging.exception('Runtime observer failed')
            except Exception:
                # The host lease remains held during transient database faults;
                # no second supervisor can race the recovering instance.
                failures += 1
                ready_seen = False
                publish(root, run_id=run_id, state='degraded')
                logging.exception('Runtime reconciliation failed')
                stop.wait(min(30.0, interval * (2 ** min(failures, 6))))
                continue
            stop.wait(interval)
        # With the exclusive host lease still held, retire this boot's
        # observation so a graceful exit cannot masquerade as readiness.


def main(argv=None):
    parser = argparse.ArgumentParser(description='Bot Control Center Pro runtime supervisor')
    parser.add_argument('--data-root', type=Path, required=True)
    parser.add_argument('--interval', type=float, default=0.5)
    parser.add_argument('--batch-size', type=int, default=8)
    args = parser.parse_args(argv)
    stop = Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    serve(args.data_root, stop=stop, interval=args.interval, batch_size=args.batch_size)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
