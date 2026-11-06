"""Regression: usable diagnostic ZIP is mandatory for Windows PAPER testing."""
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

from bcc.config import defaults
from bcc.storage import Repository
from bcc.support import export_current_report
from bcc.runtime_paths import python_environment


def test_manual_current_report_includes_hub_diagnostics_and_keeps_other_roots_out(tmp_path):
    root = tmp_path / 'test_root'
    repo = Repository(root)
    repo.create({**defaults('VELES'), 'name': 'VELES auto report'})
    (root / 'market_hub.log').write_text('hub started\nPUBLIC_CACHE: database is locked\n', encoding='utf-8')
    (root / 'ui_startup.log').write_text('Qt console: hello\n', encoding='utf-8')
    (root / 'market_hub_process.json').write_text(json.dumps({'pid': 123, 'created': 12.5, 'private_key': 'secret-value'}), encoding='utf-8')
    (tmp_path / 'another_root_secret.txt').write_text('never in output', encoding="utf-8")
    target = tmp_path / 'diagnostic.zip'
    summary = export_current_report(root, target)
    assert summary['bot_count'] == 1
    with zipfile.ZipFile(target) as z:
        assert z.testzip() is None
        names = z.namelist()
        assert 'current_bots_report/diagnostics/market_hub.log' in names
        assert 'current_bots_report/diagnostics/ui_startup.log' in names
        assert 'current_bots_report/diagnostics/market_cache_diagnostics.json' in names
        assert 'current_bots_report/diagnostics/market_hub_process.json' in names
        assert b'database is locked' in z.read('current_bots_report/diagnostics/market_hub.log')
        assert b'secret-value' not in b''.join(z.read(n) for n in names)
        assert b'never in output' not in b''.join(z.read(n) for n in names)


def test_report_now_works_after_gui_closed_even_without_bots(tmp_path):
    root = tmp_path / 'testdata'
    root.mkdir()
    (root / 'market_hub.log').write_text('Market Hub leader lease acquired\n', encoding='utf-8')
    cp = subprocess.run([sys.executable, '-m', 'tools.export_test_report', '--root', str(root)], env=python_environment(), capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert cp.returncode == 0, cp.stderr
    assert 'REPORT: ' in cp.stdout
    reports = list((root / 'REPORTS').glob('BCC_TEST_REPORT_*.zip'))
    assert len(reports) == 1
    with zipfile.ZipFile(reports[0]) as z:
        summary = json.loads(z.read('current_bots_report/report_summary.json'))
        assert summary['bot_count'] == 0
        assert summary['session_info']['automatic_after_exit'] is False
        assert b'Market Hub leader lease acquired' in z.read('current_bots_report/diagnostics/market_hub.log')


def test_failed_gui_exit_still_exports_report(tmp_path):
    root = tmp_path / 'testdata'
    repo = Repository(root)
    repo.create({**defaults('POLYMARKET'), 'name': 'POLY auto report'})
    (root / 'ui_startup.log').write_text('Simulated launch exception\n', encoding='utf-8')
    cp = subprocess.run([sys.executable, '-m', 'tools.export_test_report', '--root', str(root), '--ui-exit-code', '17'], env=python_environment(), capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert cp.returncode == 0, cp.stderr
    reports = list((root / 'REPORTS').glob('*.zip'))
    assert len(reports) == 1
    with zipfile.ZipFile(reports[0]) as z:
        summary = json.loads(z.read('current_bots_report/report_summary.json'))
        assert summary['bot_count'] == 1
        assert summary['session_info'] == {'ui_exit_code': 17, 'automatic_after_exit': True}
        assert b'Simulated launch exception' in z.read('current_bots_report/diagnostics/ui_startup.log')


def test_reporting_does_not_archive_raw_large_market_cache_or_secrets(tmp_path):
    root = tmp_path / 'testdata'
    repo = Repository(root)
    repo.create({**defaults('GRID'), 'name': 'GRID auto report'})
    (root / 'market_hub.log').write_text('a'*3000000+'\nprivate_key=ABSOLUTE_SECRET_VALUE\nLast ERROR\n', encoding='utf-8')
    (root / 'market_cache.sqlite').write_bytes(b'not a database' + b'x' * 200000)
    target = tmp_path / 'out.zip'
    export_current_report(root, target)
    with zipfile.ZipFile(target) as z:
        assert z.testzip() is None
        names = z.namelist()
        assert not any(name.endswith('market_cache.sqlite') for name in names)
        log = z.read('current_bots_report/diagnostics/market_hub.log')
        assert b'Last ERROR' in log and b'ABSOLUTE_SECRET_VALUE' not in log
        assert len(log) < 2100000
        diag = json.loads(z.read('current_bots_report/diagnostics/market_cache_diagnostics.json'))
        assert 'read_error' in diag


def test_windows_test_launcher_always_creates_report_and_points_to_folder():
    root = Path(__file__).resolve().parents[1]
    script = (root / 'TEST_START.bat').read_text(encoding='utf-8')
    assert '-m tools.export_test_report' in script
    assert 'if not "%BCC_TEST_EXIT%"=="0"' in script
    assert script.index('-m tools.export_test_report') < script.index('if not "%BCC_TEST_EXIT%"=="0"')
    assert 'explorer.exe' in script and r'TEST_DATA\REPORTS' in script
    assert (root / 'REPORT_NOW.bat').is_file()
