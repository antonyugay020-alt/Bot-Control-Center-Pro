"""Negative tests of the native acceptance verifier (no fake Windows PASS)."""
import importlib.util
import json
import struct
import zlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
import xml.etree.ElementTree as ET


def verifier():
    filename = Path(__file__).resolve().parents[1] / 'tools' / 'verify_windows.py'
    spec = importlib.util.spec_from_file_location('verify_windows_evidence_test', filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sample_png():
    # Valid 128x128 RGB PNG, rather than a forged eight-byte signature.
    def chunk(tag, content):
        return (struct.pack(">I", len(content)) + tag + content
                + struct.pack(">I", zlib.crc32(tag + content) & 0xffffffff))
    raw = (b"\x00" + b"\x20\x40\x60" * 128) * 128
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 128, 128, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def fixture(output):
    from bcc.config import STRATEGIES
    dest = output / 'screenshots'
    dest.mkdir(parents=True)
    # Synthetic fixture models the JUnit shape of a full Windows run: the
    # five Qt modules and the dashboard lifecycle test must be present.
    qt_modules = (
        "test_qt", "test_poly_indicator_qt", "test_veles_hierarchy_qt",
        "test_wizard_visual_qt", "test_settings_tree_contrast",
    )
    cases = ''.join(
        f'<testcase classname="tests.{qt_modules[n-545]}" name="test_{n}" />'
        if n >= 545 else f'<testcase classname="bcc" name="test_{n}" />'
        for n in range(550)
    ) + '<testcase classname="tests.test_native_dashboard_workflow_qt" name="test_dashboard_buttons_drive_real_paper_worker_monitor_and_restart" />'
    cases += '<testcase classname="tests.test_native_dashboard_workflow_qt" name="test_separate_qt_windows_reopen_running_account_inside_owned_acceptance_session" />'
    cases += ''.join(
        f'<testcase classname="tests.{module}" name="{name}" />'
        for module, name in (
            ('test_paper_complete_recovery_acceptance', 'test_polymarket_full_cycle_pause_reopen_official_settle_once'),
            ('test_paper_complete_recovery_acceptance', 'test_veles_full_close_open_close_on_one_sqlite_ledger'),
            ('test_process', 'test_process_stop_restart_force_and_recovery'),
        )
    )
    total = len(ET.fromstring('<testsuite>' + cases + '</testsuite>').findall('testcase'))
    cases += ''.join(
        f'<testcase classname="tests.{module}" name="{name}" />'
        for module, name in sorted(verifier().REQUIRED_INTEGRATION_CASES)
    )
    total += len(verifier().REQUIRED_INTEGRATION_CASES)
    xml = f'<testsuite tests="{total}" failures="0" errors="0" skipped="0">' + cases + '</testsuite>'
    (output / 'tests.xml').write_text(xml, encoding='utf-8')
    root = ET.fromstring(xml)
    (output / 'collected_tests.json').write_text(json.dumps({
        'schema': 1,
        'cases': [[c.get('classname'), c.get('name')] for c in root.findall('testcase')],
    }), encoding='utf-8')
    all_images = ['dashboard.png', 'all_bots_monitor.png', 'coin_picker_polymarket.png',
                  'coin_picker_dark.png', 'coin_picker_light.png',
                  'veles_finance.png', 'veles_orders.png'] + [x.lower() + '.png' for x in STRATEGIES]
    for filename in all_images:
        (dest / filename).write_bytes(sample_png())
    gui = {'status':'PASS', 'native_windows': True, 'qt_platform':'windows',
           'checks':[{'strategy':x,'status':'PASS', 'created_and_reopened':'PASS', 'edited_and_reopened':'PASS', 'deposit_adjustment_with_ledger':'PASS', 'pages':5} for x in STRATEGIES],
           'operator_cycles': [dict(strategy=x, copy='PASS', transfer='PASS',
                                    csv_history='PASS', command_revoke='PASS',
                                    delete_and_copy_isolation='PASS',
                                    paper_ledger_audit='PASS',
                                    command_scope='INTENT_ONLY_NOT_WORKER_E2E')
                               for x in ('VELES','GRID','PUMP_DUMP','POLYMARKET')],
           'all_bots_monitor':'PASS', 'all_bots_monitor_rows':4,
           'all_bots_monitor_strategies':['VELES', 'GRID', 'PUMP_DUMP', 'POLYMARKET'],
           'poly_coin_catalog':'PASS',
           'coin_picker':[{'theme':x,'status':'PASS','minimum_contrast':5.0} for x in ('light','dark')]}
    (dest / 'gui_report.json').write_text(json.dumps(gui),encoding='utf-8')
    sources = {'all_available':True, 'read_only':True,
               'at':datetime.now(timezone.utc).isoformat(),
               'checks':[{'source':x,'status':'PASS',
                          'evidence':{'book_fresh':True, 'book_age_upper':1.0}} for x in
                         ('Bybit USDT Futures', 'Polymarket Gamma + CLOB', 'Binance spot impulse')]}
    (output / 'public_sources.json').write_text(json.dumps(sources),encoding='utf-8')


def failures(out):
    m=verifier()
    return m.validate_required_evidence(out)


def test_missing_critical_evidence_fails_even_after_zero_exit(tmp_path):
    assert failures(tmp_path)
    fixture(tmp_path)
    assert failures(tmp_path) == []
    (tmp_path / 'screenshots' / 'gui_report.json').unlink()
    assert any('gui_report' in e for e in failures(tmp_path))


def test_forged_or_incomplete_native_gui_does_not_pass(tmp_path):
    fixture(tmp_path)
    path=tmp_path / 'screenshots' / 'gui_report.json'
    data=json.loads(path.read_text(encoding="utf-8"))
    data['status']='FAIL'
    path.write_text(json.dumps(data), encoding="utf-8")
    assert any('gui' in e.lower() for e in failures(tmp_path))
    data['status']='PASS'
    data['checks'].pop()
    path.write_text(json.dumps(data), encoding="utf-8")
    assert failures(tmp_path)
    data['checks'].append({'strategy':'EMA_RSI_VOLUME','status':'PASS'})
    path.write_text(json.dumps(data), encoding="utf-8")
    (tmp_path / 'screenshots' / 'dashboard.png').unlink()
    assert any('dashboard.png' in e for e in failures(tmp_path))


def test_junit_skips_failures_and_empty_run_are_rejected(tmp_path):
    fixture(tmp_path)
    path=tmp_path / 'tests.xml'
    for counts in ('tests="42" failures="0" errors="0" skipped="1"',
                   'tests="42" failures="1" errors="0" skipped="0"',
                   'tests="0" failures="0" errors="0" skipped="0"'):
        path.write_text('<testsuite '+counts+' />',encoding='utf-8')
        assert failures(tmp_path)


def test_unavailable_public_source_rejected_even_with_success_exit(tmp_path):
    fixture(tmp_path)
    path=tmp_path / 'public_sources.json'
    data=json.loads(path.read_text(encoding="utf-8"))
    data['all_available']=False
    data['checks'][0]['status']='UNAVAILABLE'
    path.write_text(json.dumps(data), encoding="utf-8")
    assert any('public' in e.lower() for e in failures(tmp_path))


def test_report_png_payload_must_be_valid_header(tmp_path):
    fixture(tmp_path)
    (tmp_path / 'screenshots' / 'veles_orders.png').write_text('failed to render', encoding="utf-8")
    assert any('veles_orders' in e for e in failures(tmp_path))


def test_stale_source_report_or_unfresh_book_does_not_pass(tmp_path):
    fixture(tmp_path)
    path=tmp_path / 'public_sources.json'
    data=json.loads(path.read_text(encoding="utf-8"))
    data['at']=(datetime.now(timezone.utc)-timedelta(hours=3)).isoformat()
    path.write_text(json.dumps(data), encoding="utf-8")
    assert failures(tmp_path)
    data['at']=datetime.now(timezone.utc).isoformat()
    data['checks'][0]['evidence']['book_fresh']=False
    path.write_text(json.dumps(data), encoding="utf-8")
    assert failures(tmp_path)


def test_native_claim_requires_create_and_edit_disk_round_trip(tmp_path):
    fixture(tmp_path)
    path=tmp_path / 'screenshots' / 'gui_report.json'
    gui=json.loads(path.read_text(encoding="utf-8"))
    gui['checks'][0]['edited_and_reopened']='NOT_RUN'
    path.write_text(json.dumps(gui),encoding='utf-8')
    assert any('конструкторы' in problem for problem in failures(tmp_path))


def test_truncated_png_with_correct_signature_is_rejected(tmp_path):
    fixture(tmp_path)
    path = tmp_path / 'screenshots' / 'dashboard.png'
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"fake")
    assert any('dashboard.png' in e for e in failures(tmp_path))


def test_png_checksum_corruption_is_rejected(tmp_path):
    fixture(tmp_path)
    path = tmp_path / 'screenshots' / 'dashboard.png'
    data = bytearray(path.read_bytes())
    data[30] ^= 1
    path.write_bytes(data)
    assert any('dashboard.png' in e for e in failures(tmp_path))


def test_png_valid_crc_but_broken_compression_is_rejected(tmp_path):
    fixture(tmp_path)
    path = tmp_path / 'screenshots' / 'dashboard.png'
    def chunk(tag, content):
        return (struct.pack(">I", len(content)) + tag + content
                + struct.pack(">I", zlib.crc32(tag + content) & 0xffffffff))
    bad = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", 128, 128, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", b"not a zlib stream")
           + chunk(b"IEND", b""))
    path.write_bytes(bad)
    assert any('dashboard.png' in e for e in failures(tmp_path))


def test_native_dashboard_e2e_required_even_when_all_older_qt_modules_pass(tmp_path):
    fixture(tmp_path)
    xml = tmp_path / 'tests.xml'
    root = ET.parse(xml).getroot()
    assert failures(tmp_path) == []
    case = next(c for c in root.findall('testcase')
                if c.get('classname') == 'tests.test_native_dashboard_workflow_qt')
    # A report whose Qt widgets passed but which skipped the actual Dashboard
    # worker -> stop/restart path is not sufficient Windows evidence.
    case.set('classname', 'tests.test_other_non_native_module')
    root.find('testcase[@classname="tests.test_other_non_native_module"]').set('name','test_unrelated')
    ET.ElementTree(root).write(xml, encoding='unicode')
    recorded = json.loads((tmp_path / 'collected_tests.json').read_text(encoding="utf-8"))
    recorded['cases'] = [(['tests.test_other_non_native_module', 'test_unrelated'] if c == ['tests.test_native_dashboard_workflow_qt', 'test_dashboard_buttons_drive_real_paper_worker_monitor_and_restart'] else c) for c in recorded['cases']]
    (tmp_path / 'collected_tests.json').write_text(json.dumps(recorded), encoding="utf-8")
    assert any('dashboard lifecycle' in err for err in failures(tmp_path))


def test_windows_operator_lifecycle_is_required_not_just_widget_screenshots(tmp_path):
    fixture(tmp_path)
    path = tmp_path / 'screenshots' / 'gui_report.json'
    baseline = json.loads(path.read_text(encoding='utf-8'))
    assert failures(tmp_path) == []
    for mutation in ('missing', 'duplicate', 'incomplete', 'false_worker_claim', 'missing_audit'):
        data = json.loads(json.dumps(baseline))
        if mutation == 'missing':
            data.pop('operator_cycles')
        elif mutation == 'duplicate':
            data['operator_cycles'][1]['strategy'] = 'VELES'
        elif mutation == 'incomplete':
            data['operator_cycles'].pop()
        elif mutation == 'false_worker_claim':
            data['operator_cycles'][0]['command_scope'] = 'WORKER_RUNNING'
        else:
            data['operator_cycles'][0]['paper_ledger_audit'] = 'NOT_RUN'
        path.write_text(json.dumps(data), encoding='utf-8')
        assert any('operator/PAPER lifecycle' in item for item in failures(tmp_path)), mutation


def test_monitor_snapshot_must_be_completed_not_screenshot_only(tmp_path):
    fixture(tmp_path)
    report = tmp_path / 'screenshots' / 'gui_report.json'
    data = json.loads(report.read_text(encoding='utf-8'))
    del data['all_bots_monitor_rows']
    report.write_text(json.dumps(data), encoding='utf-8')
    assert any('асинхронного' in item for item in failures(tmp_path))
    data['all_bots_monitor_rows'] = 4
    data['all_bots_monitor_strategies'] = ['VELES']
    report.write_text(json.dumps(data), encoding='utf-8')
    assert any('асинхронного' in item for item in failures(tmp_path))


def test_native_qt_financial_ledger_and_wizard_pages_are_mandatory(tmp_path):
    fixture(tmp_path)
    path = tmp_path / "screenshots" / "gui_report.json"
    original = json.loads(path.read_text(encoding="utf-8"))
    assert failures(tmp_path) == []
    for field, replacement in (("deposit_adjustment_with_ledger", "NOT_RUN"),
                               ("pages", 0)):
        report = json.loads(json.dumps(original))
        report["checks"][0][field] = replacement
        path.write_text(json.dumps(report), encoding="utf-8")
        assert any("PAPER Ledger" in issue for issue in failures(tmp_path))
    path.write_text(json.dumps(original), encoding="utf-8")
    assert failures(tmp_path) == []
