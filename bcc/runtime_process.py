"""Lifecycle of the owned runtime interpreter behind a Python launcher.

Popen owns a launcher handle; on Windows venv that handle can name a different
process from the interpreter publishing health. Only verified descendants with
an exact service command and data root may be terminated with that launcher.
"""
from pathlib import Path
import subprocess
from . import process_os as psutil


def service_command_matches(args, root):
    try:
        if not isinstance(args, (list, tuple)) or not all(isinstance(x, str) for x in args):
            return False
        if args.count("-m") != 1 or args.count("--data-root") != 1:
            return False
        return (args[args.index("-m") + 1] == "bcc.runtime_service"
                and Path(args[args.index("--data-root") + 1]).resolve() == Path(root).resolve())
    except (ValueError, IndexError, TypeError):
        return False


def owned_service(launcher, root, health):
    """Match a verified health observation to this still-owned process tree."""
    if launcher.poll() is not None:
        return None
    try:
        process = psutil.Process(health["pid"])
        if (abs(process.create_time() - health["created"]) > .05
                or process.status() == psutil.STATUS_ZOMBIE
                or not service_command_matches(process.cmdline(), root)):
            return None
        if process.pid == launcher.pid or any(p.pid == launcher.pid for p in process.parents()):
            return process
    except (KeyError, TypeError, ValueError, psutil.Error):
        pass
    return None


def stop_owned_service(launcher, root, *, timeout=5):
    """Stop authenticated interpreters before their launcher, retaining errors."""
    if launcher.poll() is not None:
        return
    descendants = []
    failure = None
    try:
        parent = psutil.Process(launcher.pid)
        descendants = [p for p in parent.children(recursive=True)
                       if service_command_matches(p.cmdline(), root)]
        for process in reversed(descendants):
            process.terminate()
        _, alive = psutil.wait_procs(descendants, timeout=timeout)
        for process in alive:
            # psutil.Process keeps its creation identity and rejects PID reuse.
            if service_command_matches(process.cmdline(), root):
                process.kill()
        _, alive = psutil.wait_procs(alive, timeout=timeout)
        if alive:
            failure = RuntimeError("Owned runtime interpreter did not stop")
    except psutil.NoSuchProcess:
        pass
    except psutil.Error as exc:
        failure = RuntimeError("Cannot verify runtime process tree: " + str(exc))
    finally:
        if launcher.poll() is None:
            launcher.terminate()
        try:
            launcher.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            launcher.kill()
            launcher.wait(timeout=timeout)
    if failure is not None:
        raise failure
