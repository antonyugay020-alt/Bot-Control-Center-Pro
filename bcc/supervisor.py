"""Durable desired-state supervision of OS-backed bot instances.

The supervisor orchestrates lifecycle but never writes trading/accounting state.
Ownership of financial mutations belongs exclusively to worker Store transactions.
No limit on the number of registered instances is imposed by the supervisor.
"""
from dataclasses import dataclass
from contextlib import ExitStack, contextmanager
from threading import RLock
from zlib import crc32

from .coordination import Coordinator
from .lifecycle import Lifecycle


@dataclass(frozen=True)
class ReconcileResult:
    key: str
    desired: str
    observed: str
    action: str
    error: str = ''


class Supervisor:
    def __init__(self, repo, lifecycle=None, coordinator=None):
        self.repo = repo
        self.lifecycle = lifecycle or Lifecycle(repo)
        self.coordinator = coordinator or Coordinator(repo.root / 'coordination.sqlite')
        self.coordinator.initialize()
        self._lock = RLock()
        # Fixed lock stripes bound coordination overhead independently of fleet size.
        # Each bot is always mapped to the same stripe across threads.
        self._bot_locks = tuple(RLock() for _ in range(64))
        self._inventory_iterator = None

    def _key(self, folder):
        path = self.repo.resolve(folder)
        return str(path.relative_to(self.repo.bots)).replace('\\', '/')

    def register(self, folder):
        key = self._key(folder)
        self.coordinator.register(key)
        return key

    def create_bot(self, config):
        """Publish a complete bot, then admit its identity to durable control.

        A crash between publication and admission is recovered by inventory;
        no worker is launched by this method. The registry is the sole source
        of desired-state commands, including newly created bots.
        """
        folder = self.repo.create(config)
        self.coordinator.register(self._key(folder))
        return folder

    @contextmanager
    def _operator_mutation(self, folder):
        """One admission contract for all edits that read or copy an owner.

        The OS gate serializes every config/account snapshot against STOP,
        retirement and PAPER transactions across GUI/service processes. The
        short in-process stripe prevents reconciliation of this owner during
        the operation. Discovery is allowed only for non-retiring identities.
        """
        key = self._key(folder)
        with self._owner_lock(key), self.coordinator._control_gate(key):
            self._admit_stopped_owner(key)
            yield key

    def _admit_stopped_owner(self, key):
        # Caller owns this key's command gate. A worker need not exist yet:
        # an accepted START is already a running intent and cannot be silently
        # crossed by edits, clones or transfer operations.
        if not self.coordinator.register_discovered(key):
            raise ValueError('Бот удаляется или уже удалён')
        if self.coordinator.get_registration(key)['desired'] == 'RUNNING':
            from .storage import BotBusy
            raise BotBusy('Остановите исходный бот и отмените ожидающий запуск')

    @contextmanager
    def _operator_batch(self, folders):
        """Order all local stripes before all OS gates for a multi-owner edit.

        Sorting only bot keys is insufficient: two distinct keys may map to
        the same stripe. Concurrent overlapping batches must acquire unique
        stripes by index, then OS gates by key, regardless of archive order.
        Single-owner mutations use the same order and remain independent.
        """
        keys = sorted({self._key(folder) for folder in folders})
        stripes = sorted({crc32(key.encode('utf-8')) % len(self._bot_locks)
                          for key in keys})
        with ExitStack() as owners:
            for index in stripes:
                owners.enter_context(self._bot_locks[index])
            for key in keys:
                owners.enter_context(self.coordinator._control_gate(key))
                self._admit_stopped_owner(key)
            yield keys

    def save_bot(self, folder, config, expected):
        """Edit a stopped owner using the same financial/control admission."""
        with self._operator_mutation(folder):
            self.repo.save(folder, config, expected)

    def copy_bot(self, folder, *, history=False, name=None):
        """Publish a fully isolated copy without sharing a process/ledger lease."""
        with self._operator_mutation(folder):
            copied = self.repo.copy(folder, history=history, name=name)
            self.coordinator.register(self._key(copied))
            return copied

    def export_bots(self, folders, target, *, history=True):
        """Create an owner-consistent backup through the same control gate.

        All bot snapshots share one ordered admission boundary. Per-owner
        admission would permit START/EDIT of an earlier bot while later
        accounts were still being backed up, producing a non-atomic fleet
        transfer. The transfer layer receives the canonical batch gate.
        """
        from .transfer import export_bots
        return export_bots(self.repo, folders, target, history=history,
                           source_owner_batch=self._operator_batch)

    def export_history_csv(self, folder, target, *, limit=1_000_000):
        """Publish an owner-consistent PAPER history as an atomic CSV file.

        GUI and automation must never read a live ledger outside the same
        command/financial gate as STOP, retirement and configuration changes.
        """
        import csv
        import os
        import tempfile
        from pathlib import Path
        from .storage import BotLock, Store

        target = Path(target).resolve()
        if target.suffix.lower() != '.csv' or target.is_relative_to(self.repo.bots):
            raise ValueError('Сохраните CSV вне MY_BOTS')
        with self._operator_mutation(folder), BotLock(self.repo.resolve(folder)):
            events = Store(self.repo.resolve(folder)).history(limit)
            fields = sorted({k for event in events for k in event})
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8-sig',
                                             newline='', dir=target.parent,
                                             prefix='.bcc-history-', suffix='.tmp',
                                             delete=False) as output:
                pending = Path(output.name)
                try:
                    writer = csv.DictWriter(output, fieldnames=fields)
                    writer.writeheader()
                    writer.writerows(events)
                    output.flush()
                    os.fsync(output.fileno())
                except BaseException:
                    pending.unlink(missing_ok=True)
                    raise
            try:
                os.replace(pending, target)
            finally:
                pending.unlink(missing_ok=True)
        return target

    def import_bots(self, source, *, history=True):
        """Publish an atomic transfer batch, then register its new owners.

        An interruption after successful publication is safely reconciled by
        the inventory iterator without a second copy or lost PAPER state.
        """
        from .transfer import import_bots
        folders = import_bots(self.repo, source, history=history,
                              source_owner_batch=self._operator_batch)
        for folder in folders:
            self.coordinator.register(self._key(folder))
        return folders

    def save_shared_risk_policy(self, policy):
        """Keep host policy changes inside the canonical command authority."""
        from .shared_risk import save_policy
        return save_policy(self.repo, self.lifecycle, policy,
                           coordinator=self.coordinator)

    def request_start(self, folder):
        """Durably request startup; execution belongs to the runtime service.

        Kept as a compatibility entrypoint for callers that previously invoked
        process operations from the UI. It deliberately never starts a worker.
        """
        return self.submit(folder, running=True)

    def request_stop(self, folder, *, force=False):
        """Durably request shutdown, without executing it in the caller."""
        return self.submit(folder, running=False, force=force)

    def submit(self, folder, *, running, force=False):
        """Durable command acceptance, independent of process health and inventory.

        An operator command is accepted in one SQLite transaction even if
        the worker is inaccessible or its monitoring status is unreadable.
        The independent runtime service is solely responsible for execution.
        """
        key = self._key(folder)
        desired = 'RUNNING' if running else 'STOPPED'
        self.coordinator.submit_desired(key, desired, force=force,
                                        discard_previous_exit=True)
        return ReconcileResult(key, desired, 'Не проверено', 'queued')

    def submit_drain(self, folder, *, draining):
        """Durably accept drain/resume without blocking on process health."""
        key = self._key(folder)
        self.coordinator.submit_drain_intent(key, draining)
        return ReconcileResult(key, 'DRAIN' if draining else 'RESUME',
                               'Не проверено', 'queued')

    def _owner_lock(self, key):
        return self._bot_locks[crc32(key.encode('utf-8')) % len(self._bot_locks)]

    def delete_bot(self, folder, *, discard_paper_state=False):
        """Revoke runtime authority before destructive filesystem deletion.

        A failed delete leaves the bot durably STOPPED, never resurrected by
        the reconciler. Registry removal happens only after the disk commit.
        """
        path = self.repo.resolve(folder)
        key = self._key(path)
        # The stripe serializes a concurrent in-process reconcile; the
        # transactional retirement fence protects other GUI/service processes.
        with self._owner_lock(key):
            with self._lock:
                self.coordinator.begin_retirement(key)
                self.repo.delete(path, discard_paper_state=discard_paper_state)
                self.coordinator.unregister(key)

    def submit_manual_close(self, folder):
        """Publish one atomic PAPER-close intent for the current launch epoch."""
        path = self.repo.resolve(folder)
        cfg, _ = self.repo.load(path)
        if cfg['strategy'] == 'POLYMARKET':
            raise ValueError('Polymarket позиции удерживаются до официального исхода')
        key = self._key(path)
        self.coordinator.submit_manual_close_intent(key)
        return ReconcileResult(key, 'MANUAL_CLOSE', 'Не проверено', 'queued')

    def reconcile_one(self, folder, *, force=False):
        """Compare desired and observed state; never claim success on errors."""
        with self._owner_lock(self._key(folder)):
            # Reconciliation is a read of an already registered identity.
            # Registration belongs to inventory/submit, not the high-frequency
            # service loop: a write transaction for every bot on every pass
            # would serialize the fleet behind SQLite's single writer.
            key = self._key(folder)
            row = self.coordinator.get_registration(key)
            if row is None:
                raise KeyError(f'Bot not registered: {key}')
            desired = row['desired']
            observed = self.lifecycle.status(folder)
            action = 'none'
            try:
                if desired == 'RUNNING' and observed == 'Остановлен':
                    retry = self.coordinator.restart_status(key)
                    # A successful Popen is not proof of a healthy worker. If
                    # that generation is now absent, record its crash once,
                    # then enforce durable restart backoff across service deaths.
                    if retry['spawn_epoch'] > retry['handled_exit']:
                        self.coordinator.record_unexpected_exit(key, 'Worker exited after launch')
                        retry = self.coordinator.restart_status(key)
                    if retry['retry_after'] > self.coordinator.clock():
                        return ReconcileResult(key, desired, observed, 'backoff', retry['last_error'])
                    self.lifecycle.start(folder)
                    self.coordinator.record_spawn(key)
                    action = 'start'
                elif desired == 'RUNNING' and observed == 'Работает':
                    if (row['restart_failures'] > 0
                            and row['spawn_epoch'] > row['handled_exit']
                            and row['spawned_at'] > 0
                            and row['spawned_at'] <= self.coordinator.clock() - 30):
                        self.coordinator.record_runtime_healthy(key)
                elif desired == 'STOPPED':
                    # A durable force-stop must take precedence over an earlier
                    # graceful stop that is still draining. Acknowledgement is
                    # only written after the requested transition is observed
                    # or the process has already exited.
                    forced = bool(force or row.get('force_stop', False))
                    if observed == 'Остановлен':
                        if row.get('force_stop', False):
                            self.coordinator.clear_force_stop(key, row['command_version'])
                    elif observed == 'Запуск':
                        # A pending launcher is not a verified worker.  A stop
                        # command must revoke its launch authority, not try to
                        # kill a PID from stale process.json.  If the worker
                        # holds BotLock, wait for its independently observed
                        # RUNNING/STOPPED transition and reconcile again.
                        revoke = getattr(self.lifecycle, 'revoke_pending_launch', None)
                        if revoke is not None and revoke(folder):
                            action = 'revoke-startup'
                        else:
                            action = 'await-startup'
                    elif forced or observed not in ('Остановка', 'Завершает сделки'):
                        # Lifecycle validates the worker PID and process identity.
                        self.lifecycle.stop(folder, force=forced)
                        action = 'force-stop' if forced else 'stop'
                        # Do not acknowledge a forced stop merely because the
                        # termination signal was delivered. The process may
                        # still be alive, or the host can crash before it exits.
                        # Keep intent durable until a later reconciliation
                        # independently observes the stopped state.
                # Drain/resume are also owned by the runtime process, never GUI.
                # Only acknowledge after successful idempotent command emission.
                # A crash before acknowledgement replays the latest intent.
                pending = self.coordinator.pending_drain(key, launch_revision=row['command_version'])
                if pending and desired == 'RUNNING' and self.lifecycle.active_snapshot(folder) is not None:
                    revision, draining = pending
                    if draining:
                        self.lifecycle.finish(folder)
                    else:
                        self.lifecycle.cancel_finish(folder)
                    self.coordinator.acknowledge_drain(
                        key, revision, launch_revision=row['command_version'])
                    action = 'drain' if draining else 'resume'
                final = self.lifecycle.status(folder)
                settled = ((desired == 'RUNNING' and final == 'Работает') or
                           (desired == 'STOPPED' and final == 'Остановлен'))
                self.coordinator.observe_command(
                    key, row['command_version'],
                    state='applied' if settled else 'running', observed=final)
                return ReconcileResult(key, desired, final, action)
            except Exception as exc:
                message = f'{type(exc).__name__}: {exc}'
                try:
                    self.coordinator.observe_command(
                        key, row['command_version'], state='error',
                        observed=observed, error=message)
                except Exception:
                    # A failed status write must not disguise the original error.
                    pass
                if desired == 'RUNNING':
                    self.coordinator.record_restart_failure(key, message)
                return ReconcileResult(key, desired, observed, 'error', message)

    def inventory(self):
        """Register filesystem bots separately from routine bounded reconciliation."""
        with self._lock:
            for folder, cfg in self.repo.list():
                if not cfg.get('invalid'):
                    self.coordinator.register_discovered(self._key(folder))

    def inventory_slice(self, *, limit=8):
        """Advance discovery without holding reconciliation for a full scan.

        State is local to this runtime supervisor and can be restarted after
        an OS crash. Registration is durable and idempotent in SQLite; no
        published bot is skipped permanently if discovery is interrupted.
        """
        if type(limit) is not int or limit < 1:
            raise ValueError('limit must be positive')
        with self._lock:
            if self._inventory_iterator is None:
                producer = getattr(self.repo, 'iter_inventory', self.repo.list)
                self._inventory_iterator = iter(producer())
            examined = 0
            try:
                while examined < limit:
                    try:
                        folder, cfg = next(self._inventory_iterator)
                    except StopIteration:
                        self._inventory_iterator = None
                        return examined, True
                    examined += 1
                    if not cfg.get('invalid'):
                        self.coordinator.register_discovered(self._key(folder))
            except BaseException:
                # Reconstruct a new publication snapshot after failed scans.
                self._inventory_iterator = None
                raise
            return examined, False

    def reconcile_key(self, key):
        """One isolated service action, safe for a bounded concurrent executor."""
        try:
            folder = self.repo.resolve(self.repo.bots / key)
        except (OSError, ValueError):
            return ReconcileResult(key, 'UNKNOWN', 'Отсутствует', 'orphan')
        try:
            return self.reconcile_one(folder)
        except Exception as exc:
            return ReconcileResult(key, 'UNKNOWN', 'Ошибка', 'error',
                                   f'{type(exc).__name__}: {exc}')

    def reconcile_batch(self, cursor=None, *, limit=1, sync_inventory=True):
        """Synchronous compatibility path; production service uses Fleet."""
        if type(limit) is not int or limit < 1:
            raise ValueError('limit must be positive')
        if sync_inventory:
            self.inventory()
        keys = self.coordinator.next_keys(cursor, limit=limit)
        return (keys[-1] if keys else None), [self.reconcile_key(key) for key in keys]

    def reconcile_urgent(self, cursor=None, *, limit=4):
        """Service urgent shutdowns without waiting for the full fleet sweep.

        This does not issue new commands, and it never bypasses the existing
        process-identity verification or the durable command revision.
        """
        if type(limit) is not int or limit < 1:
            raise ValueError('limit must be positive')
        results = []
        keys = self.coordinator.urgent_force_stops(cursor, limit=limit)
        for key in keys:
            try:
                folder = self.repo.resolve(self.repo.bots / key)
                results.append(self.reconcile_one(folder))
            except Exception as exc:
                results.append(ReconcileResult(key, 'STOPPED', 'Ошибка',
                                               'error', f'{type(exc).__name__}: {exc}'))
        return (keys[-1] if keys else None), results

    def reconcile_all(self):
        """Synchronize registry without creating an unbounded execution queue."""
        with self._lock:
            for folder, cfg in self.repo.list():
                if not cfg.get('invalid'):
                    self.coordinator.register_discovered(self._key(folder))
            registered = {row['key']: row for row in self.coordinator.snapshot()}
            results = []
            for key in registered:
                # A deleted bot must never be recreated by a stale desired state.
                try:
                    folder = self.repo.resolve(self.repo.bots / key)
                except (OSError, ValueError):
                    results.append(ReconcileResult(key, registered[key]['desired'], 'Отсутствует', 'orphan'))
                    continue
                results.append(self.reconcile_one(folder))
            return results
