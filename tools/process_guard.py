"""Run check subprocesses with explicit process-tree cleanup on timeout.

This module is a QA-only utility. It must never launch trading workers.
"""
import os
import signal
import subprocess
import time
import threading

from bcc import process_os as psutil


def _live_group_members(group_id):
    """Find surviving members of a newly created POSIX QA process group.

    A child reparented to init still belongs to this group. Checking only
    Popen.wait() or Process.children() after the parent exits misses it.
    """
    found = []
    for candidate in psutil.process_iter(attrs=["pid", "status"]):
        try:
            if (candidate.pid != group_id
                    and candidate.info["status"] != psutil.STATUS_ZOMBIE
                    and os.getpgid(candidate.pid) == group_id):
                found.append(candidate)
        except (ProcessLookupError, PermissionError, psutil.Error):
            continue
    return found


def _alive(processes):
    living = []
    for candidate in processes:
        try:
            if candidate.is_running() and candidate.status() != psutil.STATUS_ZOMBIE:
                living.append(candidate)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    return living


def _watch_descendants(pid, stop, observed):
    """Follow workers even if they detach from their parent's process group."""
    try:
        parent = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return
    while not stop.is_set():
        try:
            for child in parent.children(recursive=True):
                observed.setdefault(getattr(child, "pid", id(child)), child)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
        stop.wait(0.01)


def _reject_remaining_children(group_id, observed):
    """A successful QA command is not a PASS when it has left workers alive."""
    deadline = time.monotonic() + 0.5
    while True:
        # POSIX group membership survives reparenting; observed process
        # identities also detect workers started with start_new_session=True.
        group = _live_group_members(group_id) if os.name != "nt" else []
        surviving = {p.pid: p for p in _alive([*observed.values(), *group])}
        if not surviving:
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    if os.name != "nt":
        try:
            os.killpg(group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
    for child in surviving.values():
        try:
            child.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    try:
        psutil.wait_procs(list(surviving.values()), timeout=2)
    except psutil.Error:
        pass
    raise OSError("QA command exited but left live child processes: " + repr(list(surviving)))


def run_logged_command(command, *, cwd, stdout, timeout, env=None):
    options = {
        "cwd": cwd,
        "stdout": stdout,
        "stderr": subprocess.STDOUT,
        "env": env,
    }
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    proc = subprocess.Popen(command, **options)
    observed = {}
    stop = threading.Event()
    tracker = threading.Thread(
        target=_watch_descendants, args=(proc.pid, stop, observed), daemon=True
    )
    tracker.start()
    try:
        code = proc.wait(timeout=timeout)
        stop.set()
        tracker.join(timeout=0.5)
        _reject_remaining_children(proc.pid, observed)
        return code
    except subprocess.TimeoutExpired:
        stop.set()
        tracker.join(timeout=0.5)
        # Snapshot children BEFORE killing the parent: after its exit the OS
        # may reparent them and they become undiscoverable from the root PID.
        try:
            descendants = psutil.Process(proc.pid).children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            descendants = []
        descendants = list({getattr(p, "pid", id(p)): p for p in [*descendants, *observed.values()]}.values())
        # subprocess.run(timeout=...) kills only its immediate child. A pytest
        # child can leave grandchild workers alive and retain output handles.
        if os.name == "nt":
            try:
                result = subprocess.run(
                    ["taskkill.exe", "/T", "/F", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
                # taskkill can fail with a nonzero result without raising.
                # Do not leave a live check process until the next wait times out.
                if result.returncode != 0 and proc.poll() is None:
                    proc.kill()
            except (OSError, subprocess.TimeoutExpired):
                if proc.poll() is None:
                    proc.kill()
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        # SIGKILL delivery is asynchronous. Reaping the immediate child does
        # not prove that its descendants have stopped executing (a test once
        # observed state "R" after a successful killpg). Wait for the captured
        # process identities to exit, then kill stragglers. A timeout still
        # reports failure; cleanup can never convert it to PASS.
        if descendants:
            try:
                _, alive = psutil.wait_procs(descendants, timeout=3)
            except psutil.Error:
                # A diagnostic failure must not disguise the original timeout.
                alive = descendants
            for child in alive:
                try:
                    child.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    pass
            if alive:
                try:
                    psutil.wait_procs(alive, timeout=2)
                except psutil.Error:
                    pass
        raise
