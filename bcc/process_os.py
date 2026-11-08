"""Process identities in the caller's PID namespace.

Windows and ordinary Linux use psutil directly. A Linux host-mounted /proc can
use different numeric PIDs from Popen/os.kill. In that case NSpid plus the PID
namespace inode resolves inspection paths; signals still target the caller's
namespace and a pidfd pins the exact generation before delivery. A number
alone, a heartbeat, or a process name never supplies ownership authority.
"""
import os
from pathlib import Path
import signal
import sys
import threading
import time

try:
    import psutil as _psutil
except ImportError:
    _psutil = None

Error = _psutil.Error
NoSuchProcess = _psutil.NoSuchProcess
AccessDenied = _psutil.AccessDenied
ZombieProcess = _psutil.ZombieProcess
TimeoutExpired = _psutil.TimeoutExpired
STATUS_ZOMBIE = _psutil.STATUS_ZOMBIE
boot_time = _psutil.boot_time
_PROC = Path('/proc')
_CACHE = {}
_CACHE_LOCK = threading.Lock()


def _namespaced_procfs():
    if sys.platform != 'linux':
        return False
    # A failure to inspect the namespace must not silently select an unrelated
    # numeric host PID. Propagate the error instead of guessing a mapping.
    return int((_PROC / 'self/stat').read_text(encoding="utf-8", errors="surrogateescape").split(' ', 1)[0]) != os.getpid()


_NAMESPACED = _namespaced_procfs()


def _entry_local_pid(path, namespace):
    """Return a PID only for the exact caller namespace, never an ancestor."""
    if (path / 'ns/pid').stat().st_ino != namespace:
        return None
    status = (path / 'status').read_text(encoding="utf-8", errors="surrogateescape")
    values = next((line.split()[1:] for line in status.splitlines()
                   if line.startswith('NSpid:')), None)
    if not values or not all(x.isdecimal() for x in values):
        return None
    return int(values[-1])


def _probe_local_pid(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError as exc:
        raise NoSuchProcess(pid) from exc
    except PermissionError as exc:
        raise AccessDenied(pid) from exc
    except OverflowError as exc:
        raise NoSuchProcess(pid) from exc


def _resolve(pid):
    if type(pid) is not int or pid <= 0:
        raise ValueError('PID must be a positive integer')
    # Signal 0 is an OS liveness/permission probe in the caller namespace.
    # It cannot terminate a process and avoids mistaking inaccessible foreign
    # /proc entries for a missing local PID. An inaccessible LIVE local PID
    # remains AccessDenied; only the kernel's ESRCH is proof of death.
    _probe_local_pid(pid)
    namespace = (_PROC / 'self/ns/pid').stat().st_ino
    with _CACHE_LOCK:
        cached = _CACHE.get(pid)
    if cached is not None:
        try:
            if _entry_local_pid(_PROC / str(cached), namespace) == pid:
                return cached, namespace
        except OSError:
            pass
    for path in _PROC.iterdir():
        if not path.name.isdecimal():
            continue
        try:
            local_pid = _entry_local_pid(path, namespace)
        except PermissionError:
            continue
        except (FileNotFoundError, ProcessLookupError):
            continue
        if local_pid == pid:
            host_pid = int(path.name)
            with _CACHE_LOCK:
                # The cache is only a hint; every use rechecks NSpid/namespace.
                if len(_CACHE) >= 4096:
                    _CACHE.clear()
                _CACHE[pid] = host_pid
            return host_pid, namespace
    _probe_local_pid(pid)
    raise AccessDenied(pid, msg='Live PID exists but its namespace mapping cannot be verified')


class _NamespaceProcess:
    def __init__(self, pid=None):
        self.pid = os.getpid() if pid is None else pid
        self._host_pid, self._namespace = _resolve(self.pid)
        self._raw = _psutil.Process(self._host_pid)
        self._created = self._raw.create_time()
        self._assert_identity()

    @classmethod
    def _from_raw(cls, raw):
        namespace = (_PROC / 'self/ns/pid').stat().st_ino
        local_pid = _entry_local_pid(_PROC / str(raw.pid), namespace)
        if local_pid is None:
            return None
        obj = cls.__new__(cls)
        obj.pid, obj._host_pid, obj._namespace = local_pid, raw.pid, namespace
        obj._raw, obj._created = raw, raw.create_time()
        obj._assert_identity()
        return obj

    def is_running(self):
        try:
            return (self._raw.is_running()
                    and _entry_local_pid(_PROC / str(self._host_pid), self._namespace) == self.pid)
        except (FileNotFoundError, ProcessLookupError, NoSuchProcess):
            return False
        except PermissionError as exc:
            raise AccessDenied(self.pid) from exc

    def _assert_identity(self):
        if not self.is_running():
            raise NoSuchProcess(self.pid, msg='Process generation or namespace changed')

    def create_time(self):
        return self._created

    def cmdline(self):
        self._assert_identity()
        return self._raw.cmdline()

    def status(self):
        self._assert_identity()
        return self._raw.status()

    def exe(self):
        self._assert_identity()
        return self._raw.exe()

    def name(self):
        self._assert_identity()
        return self._raw.name()

    def parent(self):
        self._assert_identity()
        if self.pid == 1:
            return None  # Namespace init has no parent visible to this caller.
        raw = self._raw.parent()
        try:
            return None if raw is None else self._from_raw(raw)
        except (FileNotFoundError, ProcessLookupError, NoSuchProcess):
            return None
        except PermissionError as exc:
            raise AccessDenied(self.pid, msg='Cannot inspect parent namespace') from exc

    def parents(self):
        result, parent = [], self.parent()
        while parent is not None:
            result.append(parent)
            parent = parent.parent()
        return result

    def ppid(self):
        parent = self.parent()
        return parent.pid if parent is not None else 0

    def children(self, recursive=False):
        self._assert_identity()
        result = []
        for raw in self._raw.children(recursive=recursive):
            try:
                child = self._from_raw(raw)
                if child is not None:
                    result.append(child)
            except (FileNotFoundError, ProcessLookupError, NoSuchProcess):
                continue
        return result

    def send_signal(self, sig):
        self._assert_identity()
        descriptor = None
        try:
            if hasattr(os, 'pidfd_open') and hasattr(signal, 'pidfd_send_signal'):
                descriptor = os.pidfd_open(self.pid, 0)
                # The handle might have been opened after PID reuse. Verify the
                # saved procfs generation again while this kernel handle is held.
                self._assert_identity()
                signal.pidfd_send_signal(descriptor, sig)
            else:
                # Older Linux retains psutil's generation check before a signal
                # in the caller namespace. Never pass the host inspection PID.
                self._assert_identity()
                os.kill(self.pid, sig)
        except ProcessLookupError as exc:
            raise NoSuchProcess(self.pid) from exc
        except PermissionError as exc:
            raise AccessDenied(self.pid) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def terminate(self):
        self.send_signal(signal.SIGTERM)

    def kill(self):
        self.send_signal(signal.SIGKILL)

    def wait(self, timeout=None):
        if timeout is not None and timeout < 0:
            raise ValueError('timeout must be nonnegative')
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            # WNOWAIT obtains an owned child's exact exit status without
            # stealing it from the Popen handle that is responsible for reaping.
            try:
                result = os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                if result is not None:
                    return result.si_status if result.si_code == os.CLD_EXITED else -result.si_status
            except ChildProcessError:
                pass
            if not self.is_running() or self.status() == STATUS_ZOMBIE:
                return None
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutExpired(timeout, pid=self.pid)
            time.sleep(min(.01, max(0, deadline - time.monotonic())) if deadline else .01)


def Process(pid=None):
    return _NamespaceProcess(pid) if _NAMESPACED else _psutil.Process(pid)


def process_iter(attrs=None, ad_value=None):
    if not _NAMESPACED:
        yield from _psutil.process_iter(attrs=attrs, ad_value=ad_value)
        return
    namespace = (_PROC / 'self/ns/pid').stat().st_ino
    for path in _PROC.iterdir():
        if not path.name.isdecimal():
            continue
        try:
            local_pid = _entry_local_pid(path, namespace)
            if local_pid is None:
                continue
            process = _NamespaceProcess(local_pid)
            if attrs is not None:
                process.info = {}
                for key in attrs:
                    try:
                        process.info[key] = process.pid if key == 'pid' else getattr(process, key)()
                    except AccessDenied:
                        process.info[key] = ad_value
            yield process
        except (OSError, NoSuchProcess, AccessDenied):
            continue


def pid_exists(pid):
    try:
        return Process(pid).is_running()
    except (NoSuchProcess, ValueError):
        return False
    except AccessDenied:
        return True


def wait_procs(procs, timeout=None, callback=None):
    if not _NAMESPACED:
        return _psutil.wait_procs(procs, timeout=timeout, callback=callback)
    if timeout is not None and timeout < 0:
        raise ValueError('timeout must be nonnegative')
    if callback is not None and not callable(callback):
        raise TypeError('callback must be callable')
    gone, alive = [], list(procs)
    deadline = None if timeout is None else time.monotonic() + timeout
    while alive:
        for process in list(alive):
            try:
                stopped = not process.is_running() or process.status() == STATUS_ZOMBIE
            except NoSuchProcess:
                stopped = True
            if stopped:
                process.returncode = process.wait(timeout=0)
                alive.remove(process)
                gone.append(process)
                if callback is not None:
                    callback(process)
        if not alive or deadline is not None and time.monotonic() >= deadline:
            break
        time.sleep(.01)
    return gone, alive
