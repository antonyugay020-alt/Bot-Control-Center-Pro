"""Unattended Windows QA report; no interactive bot session or real orders."""
import json
import os
import subprocess
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from tools import manual_qa_current
from tools.acceptance_auto_report import build_report, render_report
from tools.process_guard import run_logged_command
from tools.qa_budgets import WINDOWS_QA_TIMEOUTS
from tools.verify_package import assert_same_snapshot, verify


def public_ws_check(root, output, seal):
    """Check real public Bybit book and independent mark via an owned Hub."""
    from tools.bybit_ws_live_acceptance import validate_report
    folder = output / 'bybit_ws'
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    for key in ('BCC_DATA_ROOT', 'BCC_PUBLIC_CACHE', 'BCC_MARKET_HUB_MODE',
                'BCC_BYBIT_WS_QUOTES', 'BCC_BYBIT_WS_BOOKS', 'BCC_BYBIT_WS_KLINES'):
        env.pop(key, None)
    result = {'status': 'NOT_CONFIRMED', 'scope': 'public Bybit WS book and independent mark; no orders'}
    try:
        assert_same_snapshot(seal, verify(root))
        with (output / 'bybit_ws.log').open('w', encoding='utf-8') as log:
            code = run_logged_command(
                [sys.executable, '-m', 'tools.bybit_ws_live_acceptance', '--output', str(folder), '--require-mark'],
                cwd=root, stdout=log, timeout=WINDOWS_QA_TIMEOUTS['bybit_ws'], env=env)
        evidence_path = folder / 'live_ws_report.json'
        evidence = json.loads(evidence_path.read_text(encoding='utf-8'))
        errors = validate_report(evidence, seal['manifest_sha256'])
        if code == 0 and not errors:
            result.update(status='PASS', evidence='bybit_ws/live_ws_report.json')
        else:
            result.update(status='FAIL', exit_code=code, errors=errors,
                          evidence='bybit_ws/live_ws_report.json')
    except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired) as exc:
        result.update(status='FAIL', error=f'{type(exc).__name__}: {exc}')
    return result


def public_paper_check(root, output, seal):
    """Run one bounded, independent PAPER cycle with live public data."""
    folder = output / 'live_paper'
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    for key in ('BCC_DATA_ROOT', 'BCC_PUBLIC_CACHE', 'BCC_MARKET_HUB_MODE',
                'BCC_BYBIT_WS_QUOTES', 'BCC_BYBIT_WS_BOOKS', 'BCC_BYBIT_WS_KLINES'):
        env.pop(key, None)
    result = {'status': 'NOT_CONFIRMED', 'scope': 'one public Bybit VELES PAPER cycle'}
    try:
        assert_same_snapshot(seal, verify(root))
        with (output / 'live_paper.log').open('w', encoding='utf-8') as log:
            code = run_logged_command(
                [sys.executable, '-m', 'tools.auto_live_paper', '--output', str(folder)],
                cwd=root, stdout=log, timeout=240, env=env)
        evidence_path = folder / 'live_paper_report.json'
        evidence = json.loads(evidence_path.read_text(encoding='utf-8'))
        valid = (code == 0 and evidence.get('status') == 'PAPER_CYCLE_PASS'
                 and evidence.get('real_orders') is False and evidence.get('entry') is True
                 and evidence.get('exit') is True and evidence.get('ledger') == 'PASS'
                 and evidence.get('execution_freshness', {}).get('status') == 'PASS'
                 and evidence.get('cleanup', {}).get('status') == 'STOPPED_STATE_PRESERVED'
                 and evidence.get('cleanup', {}).get('services_retired') is True)
        result.update(status='PASS' if valid else 'NOT_CONFIRMED', exit_code=code,
                      evidence='live_paper/live_paper_report.json')
    except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired) as exc:
        result.update(status='NOT_CONFIRMED', error=f'{type(exc).__name__}: {exc}')
    return result


def create_report(root, commit, seal, *, live_check=public_ws_check, paper_check=public_paper_check):
    """Collect a new report; never adopt an existing account or QA session."""
    root = Path(root)
    ident = datetime.now().strftime('%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:8]
    output = root / 'verification_reports' / ('automatic_' + ident)
    output.mkdir(parents=True, exist_ok=False)
    manual_qa_current.EXPECTED_MANIFEST = seal['manifest_sha256']
    prior = manual_qa_current.copy_existing_automated_evidence(root, output)
    # No further exchange traffic after a failed automated safety gate.
    if prior.get('reported_status') == 'AUTOMATED_PASS_MANUAL_NOT_RUN':
        live = live_check(root, output, seal)
        # Do not create a new isolated cache and recontact the provider after
        # a failed WS probe: it may have observed a rate/policy fence.
        if live.get('status') == 'PASS':
            paper = paper_check(root, output, seal)
        else:
            paper = {'status': 'NOT_RUN', 'reason': 'Public Bybit WebSocket check did not pass'}
    else:
        live = {'status': 'NOT_RUN', 'reason': 'Windows automated gate did not pass'}
        paper = {'status': 'NOT_RUN', 'reason': 'Windows automated gate did not pass'}
    report = build_report(output, manual_qa_current.CHECKS, seal['manifest_sha256'], [])
    report.update(source_commit=commit, generated_utc=datetime.now(timezone.utc).isoformat(),
                  prior_automated_qa=prior, public_bybit_ws=live, public_paper_cycle=paper)
    assert_same_snapshot(seal, verify(root))
    from bcc.storage import atomic_json
    atomic_json(output / 'AUTOMATED_ACCEPTANCE_REPORT.json', report)
    (output / 'AUTOMATED_ACCEPTANCE_REPORT_RU.txt').write_text(render_report(report), encoding='utf-8')
    # The verifier excludes BCC_TEST_REPORT_* from the immutable source set;
    # placing the one evidence archive in the QA root makes it easy to find.
    archive = root / ('BCC_TEST_REPORT_AUTOMATED_' + ident + '.zip')
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED) as package:
        for path in sorted(output.rglob('*')):
            if path.is_symlink():
                raise RuntimeError('Linked evidence path rejected: ' + str(path))
            relative = path.relative_to(output)
            if relative.parts[:2] in (('bybit_ws', 'isolated_hub'), ('live_paper', 'owned_accounts')):
                continue
            if path.is_file():
                package.write(path, relative)
    return archive, report


def main():
    if os.name != 'nt':
        raise RuntimeError('Native Windows is required for this acceptance run')
    if len(sys.argv) != 3:
        raise ValueError('Pass the dedicated QA root and its full pinned commit')
    root = manual_qa_current.validate_root(sys.argv[1], sys.argv[2])
    os.chdir(root)
    seal = verify(root)
    archive, report = create_report(root, sys.argv[2], seal)
    print('Автоматический отчёт:', archive, flush=True)
    print('Bybit public WS:', report['public_bybit_ws']['status'], flush=True)
    print('Bybit VELES PAPER cycle:', report['public_paper_cycle']['status'], flush=True)
    print('Отдельные живые PAPER-сценарии не подтверждаются только связанными автотестами.', flush=True)
    return 0 if (not report['receipt_errors'] and report['public_bybit_ws']['status'] == 'PASS'
                 and report['public_paper_cycle']['status'] == 'PASS') else 1


if __name__ == '__main__':
    raise SystemExit(main())
