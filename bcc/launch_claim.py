"""Cross-Dashboard startup handshake for one PAPER worker per bot.

Call check_launch_lease while holding the bot's OS lock. A launch claim bridges
release of the controller's lock and acquisition by its child. Do not infer
worker liveness solely from a PID: PID reuse and Windows venv redirectors exist.
"""

import math
import time
from contextlib import contextmanager
from pathlib import Path

from . import process_os as psutil

from .storage import BotBusy, BotLock, atomic_json, load_json

CLAIM_FILE = "launch.request"
STARTUP_GRACE = 120.0


def claim_path(folder):
    return Path(folder) / CLAIM_FILE


def read_claim(folder):
    path = claim_path(folder)
    if not path.exists():
        return None
    try:
        claim = load_json(path)
        if (not isinstance(claim, dict) or
                not isinstance(claim.get("token"), str) or
                len(claim["token"]) != 32 or
                not isinstance(claim.get("at"), (int, float)) or
                not math.isfinite(claim["at"])):
            raise ValueError("Некорректная запись запуска")
        return claim
    except (OSError, ValueError, TypeError) as exc:
        raise BotBusy("Запуск бота не подтверждён: повреждена запись запуска") from exc


def launcher_alive(claim):
    pid = claim.get("pid")
    created = claim.get("created")
    if type(pid) is not int or pid <= 0 or type(created) not in (int, float):
        return False
    try:
        proc = psutil.Process(pid)
        return (abs(proc.create_time() - created) < .05 and
                proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE)
    except (OSError, ValueError, psutil.NoSuchProcess, psutil.AccessDenied):
        # A non-verifiable PID must NEVER authorize an unrelated process.
        return False


def claim_pending(claim):
    """Keep an unregistered spawn fenced, but reclaim a proven-dead launcher.

    Once a launcher PID and creation-time have been published under BotLock,
    the grace timer is no longer a substitute for OS liveness. Otherwise a
    child that dies before writing process.json makes a bot unstartable for
    two minutes, including after a supervisor restart. An unregistered spawn
    still needs the full grace interval to cover the controller/child race.
    """
    pid, created = claim.get("pid"), claim.get("created")
    if type(pid) is int and pid > 0 and type(created) in (int, float) and math.isfinite(created) and created > 0:
        # AccessDenied is not proof of death. Fail closed when the launcher
        # exists but cannot be inspected (restricted Windows accounts).
        try:
            p = psutil.Process(pid)
            if abs(p.create_time() - created) < .05:
                return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
            return False  # PID reused, not our launcher
        except psutil.AccessDenied:
            return True
        except (psutil.NoSuchProcess, OSError, ValueError, OverflowError):
            return False
    return 0 <= time.time() - claim["at"] < STARTUP_GRACE


def check_launch_lease(folder):
    """Reject another launch/operation; only reclaim expired, dead claims.

    Caller MUST hold BotLock, which also rules out an unverified live worker.
    """
    claim = read_claim(folder)
    if claim is None:
        return
    if claim_pending(claim):
        raise BotBusy("Бот уже запускается из другого окна Dashboard")
    claim_path(folder).unlink()


def issue_launch(folder, token):
    if claim_path(folder).exists():
        raise BotBusy("Запись запуска уже существует")
    atomic_json(claim_path(folder), {"token": token, "at": time.time()})


def register_launcher(folder, token, pid):
    claim = read_claim(folder)
    if not claim or claim["token"] != token:
        raise BotBusy("Запуск изменился до передачи рабочему процессу")
    try:
        created = psutil.Process(pid).create_time()
    except (OSError, ValueError, psutil.NoSuchProcess, psutil.AccessDenied):
        created = None
    atomic_json(claim_path(folder), {**claim, "pid": pid, "created": created})


def verify_worker_token(folder, token):
    claim = read_claim(folder)
    if claim is None or claim["token"] != token:
        raise BotBusy("Этот запуск не принадлежит подтверждённому контроллеру")


@contextmanager
def worker_startup_lease(folder, token, *, timeout=30.0, poll=0.025):
    """Wait for the issuing controller to hand off BotLock to its worker.

    The controller MUST publish a token while holding the same OS lock, then
    spawn and register the child before releasing it. A nonblocking child
    acquisition can otherwise lose that legitimate race and exit BotBusy.
    Only a matching, still-pending token authorizes waiting; a revoked launch
    fails immediately. The worker never reads or mutates PAPER state until it
    acquires the exclusive OS lease and verifies the token *again* under it.
    """
    if type(timeout) not in (int, float) or not 0 < timeout <= STARTUP_GRACE:
        raise ValueError('Invalid worker startup handoff timeout')
    deadline = time.monotonic() + timeout
    while True:
        lease = BotLock(folder)
        try:
            lease.__enter__()
        except BotBusy as exc:
            verify_worker_token(folder, token)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BotBusy('Контроллер не передал worker блокировку запуска') from exc
            time.sleep(min(poll, remaining))
        else:
            break
    try:
        verify_worker_token(folder, token)
        yield
    finally:
        lease.__exit__(None, None, None)


def complete_launch(folder, token):
    claim = read_claim(folder)
    if claim is not None:
        if claim["token"] != token:
            raise BotBusy("Запись запуска принадлежит другому процессу")
        claim_path(folder).unlink()
