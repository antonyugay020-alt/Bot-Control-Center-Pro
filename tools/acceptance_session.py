"""Own the lifetime of one NEW interactive QA workspace, outside its Qt UI.

Ordinary dashboard windows deliberately do not own the trading service. A
manual checker must therefore own its own guardian before opening that UI,
revoke its accounts after the UI exits, and retire only its private services.
Closing production windows retains the existing independent runtime behavior.
"""
import subprocess
import sys
import time
import uuid
from pathlib import Path
from threading import Event, Thread

from bcc import process_os as psutil
from bcc.hub_supervisor import _matches
from bcc.launch_claim import check_launch_lease
from bcc.runtime_health import wait_ready
from bcc.runtime_service import HostLease
from bcc.service_guard import supervise
from bcc.storage import BotBusy, BotLock, Repository, atomic_json, load_json
from bcc.supervisor import Supervisor


class AcceptanceSession:
    def __init__(self, root, output, *, python=None, timeout=30):
        if type(timeout) not in (int, float) or not 0 < timeout <= 300:
            raise ValueError('Invalid acceptance shutdown timeout')
        self.root = Path(root).absolute()
        self.output = Path(output).resolve()
        self.python = str(python or sys.executable)
        self.timeout = timeout
        self.token = uuid.uuid4().hex
        self.stop = Event()
        self.thread = None
        self.failures = []
        self.report = dict(status='STARTING', release_ready=False,
                           manual_acceptance='NOT_CONFIRMED', bots_stopped=0,
                           errors=[], data_root=str(self.root))
        self.created = False

    def _assert_owned(self):
        value = load_json(self.root / 'acceptance-session.json')
        if (self.root.is_symlink() or self.root.resolve() != self.root
                or not isinstance(value, dict)
                or value.get('session_id') != self.token
                or value.get('data_root') != str(self.root)):
            raise RuntimeError('Acceptance workspace identity changed; refusing operations')

    def _guard(self):
        def spawn(arguments, **options):
            # windows_check itself can run under system Python; the runtime
            # must use the same isolated environment as the checked Qt app.
            return subprocess.Popen([self.python, *arguments[1:]], **options)
        try:
            supervise(self.root, stop=self.stop, spawn=spawn,
                      log_path=self.output / 'runtime.log')
        except BaseException as exc:
            self.failures.append(f'{type(exc).__name__}: {exc}')

    def __enter__(self):
        # Never adopt an existing root, even an empty directory. A filename
        # or command-line flag is not authorization to stop existing accounts.
        if self.root.is_symlink():
            raise ValueError('A linked acceptance root is not allowed')
        self.root.mkdir(parents=True, exist_ok=False)
        self.root = self.root.resolve()
        self.created = True
        self.output.mkdir(parents=True, exist_ok=False)
        self.report['data_root'] = str(self.root)
        atomic_json(self.root / 'acceptance-session.json',
                    dict(schema=1, session_id=self.token, data_root=str(self.root)))
        self.owner = Supervisor(Repository(self.root))
        self.thread = Thread(target=self._guard, name='bcc-acceptance-guardian', daemon=True)
        self.thread.start()
        try:
            wait_ready(self.root, timeout=self.timeout, poll=.05)
            if self.failures:
                raise RuntimeError('; '.join(self.failures))
            self.report['status'] = 'RUNNING'
            return self
        except BaseException:
            self.stop.set()
            self.thread.join(timeout=15)
            self.report.update(status='STARTUP_FAILED', errors=list(self.failures))
            atomic_json(self.output / 'session.json', self.report)
            raise

    def _stop_accounts(self):
        self._assert_owned()
        # Include registry entries even when a saved config became invalid.
        # Revoke pending START before waiting for any PID/lock observation.
        self.owner.inventory()
        keys = [row['key'] for row in self.owner.coordinator.snapshot()]
        for key in keys:
            current = self.owner.coordinator.get_registration(key)
            if not current['retiring']:
                self.owner.coordinator.submit_desired(key, 'STOPPED', discard_previous_exit=True)
            elif current['desired'] != 'STOPPED':
                raise RuntimeError('Retiring acceptance account still requests RUNNING')
        pending = self._wait_accounts(set(keys), self.timeout)
        if pending:
            self.report['forced_cleanup'] = True
            for key in pending:
                current = self.owner.coordinator.get_registration(key)
                if current and not current['retiring']:
                    self.owner.coordinator.submit_desired(key, 'STOPPED', force=True)
            remaining = self._wait_accounts(pending, 10)
            raise RuntimeError('PAPER workers exceeded the acceptance shutdown deadline; '
                               'forced cleanup requested; remaining: ' + ', '.join(sorted(remaining)))
        self.report['bots_stopped'] = len(keys)

    def _wait_accounts(self, pending, seconds):
        until = time.monotonic() + seconds
        while pending:
            self._assert_owned()
            for key in tuple(pending):
                folder = self.root / 'MY_BOTS' / key
                if not folder.exists():
                    # Retirement can legitimately remove an account in the UI.
                    pending.remove(key)
                    continue
                try:
                    with BotLock(folder, allow_deletion_recovery=True):
                        check_launch_lease(folder)
                        if self.owner.lifecycle.process(folder) is None:
                            pending.remove(key)
                except BotBusy:
                    pass
            if not pending:
                break
            if time.monotonic() >= until:
                break
            if self.failures:
                raise RuntimeError('Acceptance guardian failed: ' + '; '.join(self.failures))
            time.sleep(.05)
        return pending

    def _stop_hub(self):
        self._assert_owned()
        path = self.root / 'market_hub_process.json'
        if not path.exists():
            return
        record = load_json(path)
        if not isinstance(record, dict):
            raise RuntimeError('Cannot verify acceptance Market Data Hub')
        try:
            process = psutil.Process(record['pid'])
            if abs(process.create_time() - record['created']) >= .05:
                return  # Old PID was reused; it belongs to someone else now.
            if process.status() == psutil.STATUS_ZOMBIE:
                return
            if not _matches(record, self.root / 'market_cache.sqlite'):
                raise RuntimeError('Cannot verify acceptance Market Data Hub')
            process.terminate()
            _, alive = psutil.wait_procs([process], timeout=5)
            for candidate in alive:
                # Process retains its birth identity; no numeric-PID fallback.
                candidate.kill()
            _, alive = psutil.wait_procs(alive, timeout=5)
            if alive:
                raise RuntimeError('Acceptance Market Data Hub did not stop')
        except psutil.NoSuchProcess:
            pass

    def __exit__(self, kind, error, traceback):
        try:
            self._stop_accounts()
            self._stop_hub()
        except BaseException as exc:
            self.report['errors'].append(f'{type(exc).__name__}: {exc}')
        finally:
            self.stop.set()
            self.thread.join(timeout=15)
            if self.thread.is_alive():
                self.report['errors'].append('Acceptance guardian did not stop')
            try:
                self._assert_owned()
                with HostLease(self.root / '.runtime_guard.lock'), HostLease(self.root / '.runtime_supervisor.lock'):
                    self.report['services_retired'] = True
            except BaseException as exc:
                self.report['services_retired'] = False
                self.report['errors'].append(f'{type(exc).__name__}: {exc}')
            self.report['errors'].extend(self.failures)
            if error is not None:
                self.report['errors'].append(f'{kind.__name__}: {error}')
            self.report['status'] = 'FAILED' if self.report['errors'] else 'STOPPED_STATE_PRESERVED'
            atomic_json(self.output / 'session.json', self.report)
        if error is None and self.report['errors']:
            raise RuntimeError('; '.join(self.report['errors']))
        return False
