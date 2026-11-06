"""Consoleless Windows launch with a visible, recoverable error report."""

import os
import sys
import tempfile
import traceback
from pathlib import Path

try:
    import subprocess
    import importlib.util

    # Import the application first: a dependency/Qt startup failure must be
    # attributed to the application, not concealed by guardian bootstrap.
    from app import main

    # app.main() performs the single canonical runtime bootstrap.
    # The consoleless entry point must not launch a second guardian.
    result = main()
    # A consoleless launcher must not silently swallow an abnormal Qt exit.
    if result:
        raise RuntimeError(f"Qt application returned nonzero exit status: {result}")
    sys.exit(0)
except Exception:
    # Installed application directories may be read-only on Windows.
    # Diagnostic reporting must not mask the original startup exception.
    candidates = [
        Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "BotControlCenterPro",
        Path(tempfile.gettempdir()) / "BotControlCenterPro",
    ]
    path = None
    for folder in candidates:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / "startup.log"
            target.write_text(traceback.format_exc(), encoding="utf-8")
            path = target
            break
        except OSError:
            continue
    if sys.platform == "win32":
        # Python.exe is used by the automated tests, where a blocking native
        # MessageBox would stall the entire suite. Only the actual *consoleless*
        # interpreter launched by START.bat may display a modal dialog.
        if Path(sys.executable).name.casefold() == "pythonw.exe":
            import ctypes

            summary = traceback.format_exc().splitlines()[-1]
            try:
                ctypes.windll.user32.MessageBoxW(
                    None,
                    "Не удалось открыть программу: " + summary
                    + "\n\nЖурнал: "
                    + (str(path) if path is not None else "недоступен")
                    + "\n\nПроверьте журнал запуска. INSTALL.bat нужен только при ошибках зависимостей.",
                    "Bot Control Center Pro",
                    0x10,
                )
            except OSError:
                pass  # The original traceback remains available in startup.log.
        # A startup failure must never be reported as a successful exit.
        raise SystemExit(1)
    raise
