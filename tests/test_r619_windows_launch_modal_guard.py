"""Windows launch regression: pytest subprocesses cannot display a blocking dialog."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from bcc.runtime_paths import python_environment

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("bad_main", ["raise RuntimeError('simulated failure')", "return 7"])
def test_console_launcher_fails_without_blocking_messagebox(tmp_path, bad_main):
    folder = tmp_path / "simulated failure"
    folder.mkdir()
    (folder / "launch.pyw").write_bytes((ROOT / "launch.pyw").read_bytes())
    action = "def main():\n    " + bad_main + "\n"
    (folder / "app.py").write_text(action, encoding="utf-8")
    data = tmp_path / "localappdata"
    environment = dict(os.environ, LOCALAPPDATA=str(data))
    try:
        proc = subprocess.run(
            [sys.executable, str(folder / "launch.pyw")],
            cwd=folder, env=python_environment(environment), capture_output=True,
            text=True, encoding='utf-8', timeout=5,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("Launch displayed a blocking modal or did not exit")
    assert proc.returncode != 0
    assert (data / "BotControlCenterPro" / "startup.log").exists()


def test_windows_qacheck_modal_is_only_in_pythonw_launch():
    source = (ROOT / "launch.pyw").read_text(encoding="utf-8")
    assert 'Path(sys.executable).name.casefold() == "pythonw.exe"' in source
    assert 'raise SystemExit(1)' in source
    assert 'import ctypes' in source
    assert 'INSTALL.bat нужен только при ошибках зависимостей' in source
