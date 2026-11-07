"""Start and reuse one market-data producer per repository; fail closed.

A short OS file lock protects check-and-spawn across shell instances. The hub
is deliberately independent of the GUI lifetime and is recovered at next start.
"""
import errno
import json
import math
import os
import sqlite3
import subprocess
from subprocess import Popen as _spawn_hub
import sys
import time
import uuid
from pathlib import Path

from . import process_os as psutil

from .storage import atomic_json
from .runtime_paths import python_environment


class HubUnavailable(RuntimeError):
    pass


def _locked(path, *, timeout=8.0):
    """Bounded cross-platform spawn lock; always close the file on failure.

    Waiting for a crashed or stalled sibling's filesystem lock must never
    freeze the dashboard indefinitely. Both Windows and POSIX use a
    nonblocking attempt and a monotonic deadline.
    """
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Market Data Hub lock timeout must be positive and finite")

    class Lock:
        def __enter__(self):
            self.f = open(path, "a+b")
            try:
                if self.f.seek(0, 2) == 0:
                    self.f.write(b"0")
                    self.f.flush()
                deadline = time.monotonic() + timeout
                while True:
                    self.f.seek(0)
                    try:
                        if os.name == "nt":
                            import msvcrt
                            msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(self.f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        return self
                    except OSError as exc:
                        if exc.errno not in (errno.EACCES, errno.EAGAIN):
                            raise
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise HubUnavailable("Market Data Hub spawn lock timed out") from exc
                        time.sleep(min(0.05, remaining))
            except BaseException:
                self.f.close()
                raise

        def __exit__(self, *_):
            try:
                if os.name == "nt":
                    import msvcrt
                    self.f.seek(0)
                    msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.f.fileno(), fcntl.LOCK_UN)
            finally:
                self.f.close()
    return Lock()


def _matches(record, cache):
    try:
        p = psutil.Process(record['pid'])
        cmd = p.cmdline()
        return (p.is_running() and p.status() != psutil.STATUS_ZOMBIE
                and abs(p.create_time() - record['created']) < .05
                and '--cache' in cmd
                and Path(cmd[cmd.index('--cache') + 1]).resolve() == cache
                and 'tools.market_hub_service' in cmd)
    except (OSError, ValueError, KeyError, IndexError, TypeError, psutil.Error):
        return False


def _verified_redirected_hub_pid(pid, cache, ready_path, challenge, started):
    """Identify the actual Python child behind a Windows venv launcher.

    On Windows a venv python.exe may be a redirector with a different PID
    from the interpreter executing the Hub. A receipt from that interpreter
    is acceptable only when its live OS command line contains the *same*
    unguessable launch challenge and exact public cache / receipt paths.
    """
    if type(pid) is not int or pid <= 0:
        return None
    try:
        p = psutil.Process(pid)
        if not p.is_running() or p.status() == psutil.STATUS_ZOMBIE:
            return None
        created = p.create_time()
        if not math.isfinite(created) or created < started - 2 or created > time.time() + 1:
            return None
        cmd = p.cmdline()
        if (not isinstance(cmd, (list, tuple)) or
                cmd.count('tools.market_hub_service') != 1 or
                any(cmd.count(flag) != 1 for flag in ('--cache', '--ready-file', '--ready-token'))):
            return None
        values = {
            flag: cmd[cmd.index(flag) + 1]
            for flag in ('--cache', '--ready-file', '--ready-token')
        }
        if (Path(values['--cache']).resolve() != cache or
                Path(values['--ready-file']).resolve() != ready_path or
                values['--ready-token'] != challenge):
            return None
        return created
    except (OSError, ValueError, IndexError, TypeError, psutil.Error):
        return None


def _hub_startup_details(root, proc):
    """Bounded diagnostic from *this* spawned public-data producer only."""
    path = root / 'market_hub.log'
    try:
        # Never attach a whole log or unrelated user files to a failure.
        with path.open('rb') as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 3072))
            tail = stream.read().decode('utf-8', errors='replace')
    except OSError:
        tail = '<hub log unavailable>'
    return 'pid={} exit={} last_log={}'.format(proc.pid, proc.poll(), repr(tail[-2000:]))


def _revoke_dead_owner(registry, record):
    """Revoke only the proven dead producer's exact SQLite lease generation.

    A PID prefix is not a generation: after Windows PID reuse a different,
    healthy Hub can own the same numeric PID. Legacy PID-only records cannot
    safely authorize early revocation and must wait for natural lease expiry.
    """
    pid = record.get('pid')
    owner = record.get('leader_owner')
    if (type(pid) is not int or pid <= 0 or type(owner) is not str
            or not owner.startswith(str(pid) + ':')
            or len(owner.split(':', 1)[1]) != 32
            or any(ch not in '0123456789abcdef' for ch in owner.split(':', 1)[1])):
        return False
    with registry._connect() as db:
        cursor = db.execute('DELETE FROM hub_leader WHERE owner=?', (owner,))
        return cursor.rowcount == 1


def ensure_hub(repo_root, *, timeout=8):
    root = Path(repo_root).resolve()
    cache = root / 'market_cache.sqlite'
    lock = root / '.market_hub_spawn.lock'
    record_path = root / 'market_hub_process.json'
    from .market_hub import HubRegistry
    with _locked(lock, timeout=timeout):
        try:
            record = json.loads(record_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            record = {}
        # A syntactically valid JSON document is not necessarily a process
        # record. Treat broken metadata as untrusted and recover from the
        # actual leader lease, rather than crashing on record.get().
        if not isinstance(record, dict):
            record = {}
        if _matches(record, cache):
            # A matching PID only proves process identity, not that the
            # producer still owns a fresh leader lease or serves data. Never
            # silently hand a worker a dead/hung hub endpoint.
            try:
                registry = HubRegistry(cache)
                with registry._connect() as db:
                    leader = db.execute(
                        'SELECT owner, expires, lease_token FROM hub_leader WHERE singleton=1'
                    ).fetchone()
                expected_owner = record.get('leader_owner')
                owner_matches = (leader and type(leader[0]) is str and (
                    leader[0] == expected_owner if type(expected_owner) is str
                    else leader[0].startswith(str(record['pid']) + ':')))
                if (owner_matches and type(leader[1]) in (int, float) and
                        math.isfinite(leader[1]) and leader[1] > time.time()
                        and type(leader[2]) is str and bool(leader[2])):
                    return record['pid']
            except (OSError, ValueError, sqlite3.Error) as exc:
                raise HubUnavailable('Market Data Hub leadership check failed') from exc
            raise HubUnavailable(
                'Market Data Hub process exists but has no valid leadership lease'
            )
        # A crashed producer may leave a 120-second database lease. Revoke it
        # only if its recorded PID and creation time no longer identify a live
        # producer. Never revoke a live but slow Hub's lease.
        if record.get('pid') and not _matches(record, cache):
            try:
                old = psutil.Process(record['pid'])
                dead = old.status() == psutil.STATUS_ZOMBIE or abs(old.create_time() - record['created']) >= .05
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                dead = True
            except (psutil.AccessDenied, KeyError, TypeError, ValueError):
                dead = False
            if dead:
                registry = HubRegistry(cache)
                _revoke_dead_owner(registry, record)
        # If the PID file was lost/corrupted while an old producer still
        # holds leadership, do not overwrite its identity with a speculative
        # second Hub. A live database lease is not enough to authorize using
        # that process: its creation time and command must also be proven.
        # A dead verified predecessor's lease was revoked above.
        registry = HubRegistry(cache)
        with registry._connect() as db:
            leader = db.execute(
                'SELECT owner, expires FROM hub_leader WHERE singleton=1'
            ).fetchone()
        if (leader and isinstance(leader[0], str)
                and type(leader[1]) in (int, float)
                and math.isfinite(leader[1]) and leader[1] > time.time()):
            raise HubUnavailable(
                'Market Data Hub has an active leader but its process identity '
                'cannot be verified; refusing to replace it'
            )
        app_root = Path(__file__).resolve().parent.parent
        env = python_environment()
        env['PYTHONPATH'] = str(app_root)
        # The hub is the *producer*. Never inherit a worker's opt-in
        # client mode or another cache path via a parent GUI/test shell.
        env.pop('BCC_MARKET_HUB_MODE', None)
        env.pop('BCC_PUBLIC_CACHE', None)
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        # A unique child-authored readiness receipt avoids interpreting a
        # momentarily locked SQLite WAL as a dead producer on native Windows.
        # The receipt is created only AFTER the producer itself owns the lease.
        challenge = uuid.uuid4().hex
        ready_path = root / ('.market_hub_ready_' + challenge + '.json')
        spawned_at = time.time()
        with (root / 'market_hub.log').open('ab') as log:
            proc = _spawn_hub([sys.executable, '-m', 'tools.market_hub_service',
                                     '--cache', str(cache), '--ready-file', str(ready_path),
                                     '--ready-token', challenge], cwd=app_root, env=env,
                                    stdout=log, stderr=subprocess.STDOUT,
                                    creationflags=flags,
                                    start_new_session=os.name != 'nt')
        verified_child = None
        try:
            created = psutil.Process(proc.pid).create_time()
            atomic_json(record_path, {'pid': proc.pid, 'created': created})
            # Windows cold startup (antivirus, .venv, SQLite WAL creation)
            # may take longer than the spawn-lock acquisition budget. Keep
            # both waits bounded, but do not confuse them into one deadline.
            startup_budget = max(timeout, 18) if os.name == 'nt' else timeout
            deadline = time.monotonic() + startup_budget
            # Reuse the registry opened before spawn: repeatedly creating it
            # while the child opens the same WAL database was an unnecessary
            # cross-process schema contention point on Windows.
            while time.monotonic() < deadline:
                # Read the authenticated proof BEFORE checking the launcher:
                # a Windows .venv redirector can exit after spawning the real
                # interpreter, with a different PID and valid service lease.
                try:
                    proof = json.loads(ready_path.read_text(encoding='utf-8'))
                    if (type(proof) is dict and proof.get('token') == challenge
                            and type(proof.get('pid')) is int
                            and type(proof.get('leader_owner')) is str):
                        service_pid = proof['pid']
                        if service_pid == proc.pid:
                            service_created = psutil.Process(service_pid).create_time()
                            valid = abs(service_created - created) < .05
                        else:
                            service_created = _verified_redirected_hub_pid(
                                service_pid, cache, ready_path, challenge, spawned_at)
                            valid = service_created is not None
                        if valid:
                            owner = proof['leader_owner']
                            # Authenticate both the OS process and the actual
                            # live SQLite lease before advertising readiness.
                            # A stale receipt cannot certify a different Hub.
                            if not owner.startswith(str(service_pid) + ':'):
                                time.sleep(.1)
                                continue
                            with registry._connect() as db:
                                lease = db.execute(
                                    'SELECT owner,expires,lease_token FROM hub_leader '
                                    'WHERE singleton=1'
                                ).fetchone()
                            if not (lease and lease[0] == owner
                                    and type(lease[1]) in (int, float)
                                    and math.isfinite(lease[1])
                                    and lease[1] > time.time()
                                    and type(lease[2]) is str and bool(lease[2])):
                                time.sleep(.1)
                                continue
                            verified_child = (service_pid, service_created)
                            atomic_json(record_path, {
                                'pid': service_pid, 'created': service_created,
                                'leader_owner': owner,
                            })
                            return service_pid
                except (OSError, ValueError, TypeError, sqlite3.Error, psutil.Error):
                    pass
                if proc.poll() is not None and proc.returncode != 0:
                    raise HubUnavailable('Market Data Hub launcher failed: ' + _hub_startup_details(root, proc))
                # A child started by this version MUST issue the handshake.
                # Returning on a separate DB poll could race the child's
                # receipt publication, leaving stale readiness artifacts.
                time.sleep(.1)
            raise HubUnavailable('Market Data Hub did not become ready: ' +
                                 _hub_startup_details(root, proc))
        except BaseException:
            # If the launcher was a Windows redirector and metadata failed
            # after a valid receipt, stop only its authenticated child, never
            # another unrelated process that happens to reuse the old PID.
            if verified_child is not None and verified_child[0] != proc.pid:
                try:
                    owned = psutil.Process(verified_child[0])
                    if abs(owned.create_time() - verified_child[1]) < .05:
                        owned.terminate()
                        owned.wait(timeout=3)
                except psutil.TimeoutExpired:
                    try:
                        if abs(owned.create_time() - verified_child[1]) < .05:
                            owned.kill()
                    except psutil.Error:
                        pass
                except (OSError, psutil.Error):
                    pass
            if proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
                except ProcessLookupError:
                    pass
            record_path.unlink(missing_ok=True)
            raise
        finally:
            # Receipt is an ephemeral, non-secret handshake artifact; never
            # let a previous child confer readiness upon a later launch.
            ready_path.unlink(missing_ok=True)
