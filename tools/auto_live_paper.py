"""Bounded real-public-data PAPER smoke test in a newly owned account root."""
import argparse
import json
import time
import traceback
from pathlib import Path

from bcc.config import condition, defaults, group, range_section, validate
from bcc.storage import Store, atomic_json, paper_account_present
from bcc.support import execution_freshness, export_current_report
from tools.acceptance_session import AcceptanceSession


def config():
    cfg = defaults('VELES')
    cfg.update(name='Automatic public PAPER QA', symbols=['BTCUSDT'], capital=500.0)
    cfg['settings']['orders']['count'] = 1
    section = range_section(30, 40)
    entry = condition('PRICE', 1e12, '<', 1)
    entry['trigger'] = 'intrabar'
    exit_signal = condition('PRICE', 1e12, '>', 1)
    exit_signal['trigger'] = 'intrabar'
    section['entry'], section['exit'] = group(entry), group(exit_signal)
    cfg['sections'] = [section]
    assert cfg['mode'] == 'PAPER' and cfg['exchange'] == 'BYBIT_LINEAR'
    validate(cfg)
    return cfg


def run(output, *, duration=120):
    output = Path(output).resolve()
    if output.exists() or not 15 <= duration <= 180:
        raise ValueError('Fresh output and bounded duration are required')
    # The session exclusively creates both directories and terminates only
    # its authenticated worker, runtime and public-data Hub on exit.
    workspace = output / 'owned_accounts'
    receipts = output / 'session'
    report = {'status': 'NOT_CONFIRMED', 'scope': 'public Bybit / VELES PAPER',
              'real_orders': False, 'release_ready': False, 'entry': False,
              'exit': False, 'ledger': 'NOT_CONFIRMED', 'account_ready': False}
    try:
        with AcceptanceSession(workspace, receipts, timeout=30) as session:
            try:
                folder = session.owner.create_bot(config())
                session.owner.request_start(folder)
                store = Store(folder)
                deadline = time.monotonic() + duration
                draining = False
                closing = False
                while time.monotonic() < deadline:
                    # request_start queues a command; it does not synchronously
                    # launch the worker or create its PAPER ledger. Only the
                    # worker creates that account, under its startup lease. Never
                    # turn missing/corrupt previously initialized state into an
                    # empty account, and never read an in-progress initialisation.
                    if not paper_account_present(folder) or not store.marker.is_file():
                        time.sleep(.5)
                        continue
                    report['account_ready'] = True
                    state = store.read(include_charts=False)
                    events = store.history(limit=500)
                    report['entry'] = any(event.get('action') == 'OPEN' for event in events)
                    report['exit'] = any(event.get('action') == 'CLOSE' for event in events)
                    if report['entry'] and not draining:
                        session.owner.submit_drain(folder, draining=True)
                        draining = True
                    if draining and state.get('strategy', {}).get('draining') and not closing:
                        session.owner.submit_manual_close(folder)
                        closing = True
                    if closing and report['exit'] and not state.get('positions'):
                        store.audit()
                        freshness = execution_freshness(events)
                        report['ledger'] = 'PASS'
                        report['execution_freshness'] = freshness
                        report['status'] = ('PAPER_CYCLE_PASS' if freshness.get('status') == 'PASS'
                                            else 'NOT_CONFIRMED')
                        break
                    time.sleep(.5)
                if not report['account_ready']:
                    report['reason'] = 'Worker PAPER account was not ready before the deadline'
                elif not report['entry']:
                    report['reason'] = 'No PAPER entry before the deadline'
                elif not report['exit']:
                    report['reason'] = 'No completed PAPER exit before the deadline'
            finally:
                try:
                    export_current_report(workspace, output / 'paper_snapshot.zip', allow_empty=True,
                                          session_info={'automatic_qa': True, 'read_only': True})
                except Exception as exc:
                    report['snapshot_error'] = f'{type(exc).__name__}: {exc}'
                    report['status'] = 'NOT_CONFIRMED'
        cleanup = json.loads((receipts / 'session.json').read_text(encoding='utf-8'))
        report['cleanup'] = {'status': cleanup.get('status'),
                             'services_retired': cleanup.get('services_retired')}
        if cleanup.get('status') != 'STOPPED_STATE_PRESERVED' or cleanup.get('services_retired') is not True:
            report['status'] = 'NOT_CONFIRMED'
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
        report['traceback'] = traceback.format_exc()[-12000:]
        report['status'] = 'NOT_CONFIRMED'
    # A missing signal or inaccessible source is a recorded non-pass, never a
    # synthetic substitute or a reason to disable portfolio safeguards.
    output.mkdir(parents=True, exist_ok=True)
    atomic_json(output / 'live_paper_report.json', report)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--duration', type=int, default=120)
    args = parser.parse_args()
    report = run(args.output, duration=args.duration)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report['status'] == 'PAPER_CYCLE_PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
