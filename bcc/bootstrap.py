"""Single verified runtime bootstrap for every official GUI entry point.

Qt never owns trading processes. Both the consoleless launcher and a directly
started app.py must establish the same independent runtime before exposing the
UI. An already verified service is reused; concurrent launches are safe because
both guardian and runtime hold OS-level host leases.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .runtime_health import observe, wait_ready
from .runtime_paths import code_root, python_environment


def ensure_runtime(root, *, spawn=None, timeout=15):
    root = Path(root).resolve()
    if observe(root) is not None:
        return observe(root)
    root.mkdir(parents=True, exist_ok=True)
    log_dir = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "BotControlCenterPro"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "runtime_service.log"
    spawn = spawn or subprocess.Popen
    with log_path.open("ab") as log:
        spawn([sys.executable, "-m", "bcc.service_guard", "--data-root", str(root),
               "--log-path", str(log_path)],
              cwd=str(code_root()), stdin=subprocess.DEVNULL,
              stdout=log, stderr=subprocess.STDOUT,
              env=python_environment(),
              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
              close_fds=True)
    return wait_ready(root, timeout=timeout)
