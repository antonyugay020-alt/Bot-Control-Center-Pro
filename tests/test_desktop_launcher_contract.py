"""Ensure the normal Windows desktop entrypoint is independent of AI development."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_desktop_start_is_independent_of_codex_and_installs_qt():
    value = (ROOT / 'START.bat').read_text(encoding='utf-8').lower()
    assert 'call "%~dp0install.bat"' in value
    assert 'launch.pyw' in value
    assert 'start "" "%pyw%"' in value
    assert 'ai_cycle_windows.bat' not in value
    assert 'codex exec' not in value


def test_installer_targets_private_venv():
    value = (ROOT / 'INSTALL.bat').read_text(encoding='utf-8').lower()
    assert 'set "venv=%~dp0.venv"' in value
    assert '"%venv%\\scripts\\python.exe" -m pip install' in value
    assert 'bcc_install_no_pause' in value


def test_diagnostic_launcher_runs_real_gui_and_logs_errors():
    value = (ROOT / 'START_DIAGNOSTIC.bat').read_text(encoding='utf-8').lower()
    assert 'launch.pyw' in value
    assert 'startup_console.log' in value
    assert 'faulthandler' in value


def test_paper_launcher_uses_separate_persistent_profile_and_normal_entrypoint():
    value = (ROOT / 'START_PAPER.bat').read_text(encoding='utf-8').lower()
    assert 'setlocal enableextensions disabledelayedexpansion' in value
    assert 'set "bcc_data_root=%~dp0..\\bcc_v39_paper_data"' in value
    assert 'call "%~dp0start.bat"' in value
    assert 'check_windows.bat' not in value


def test_desktop_launch_checks_the_public_ws_runtime():
    for name in ('START.bat', 'INSTALL.bat'):
        content = (ROOT / name).read_text(encoding='utf-8')
        assert 'import PySide6, requests, psutil, websocket' in content
