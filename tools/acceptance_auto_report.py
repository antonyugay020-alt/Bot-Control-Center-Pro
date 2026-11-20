"""Report related automated evidence without inventing live acceptance.

JUnit tests and read-only session snapshots are different kinds of evidence.
Passing related tests never marks a full live-market criterion as accepted.
"""
import json
import zipfile
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree


# Related test groups, not a claim that every requirement in the title is
# covered by those groups. The report retains the identities and counts.
RELATED = {
    'display': ('test_wizard_visual_qt', 'test_native_dashboard_workflow_qt'),
    'constructors': ('test_wizard_all_strategies_publication_run28', 'test_constructor_roundtrip_integration'),
    'veles_controls': ('test_veles_fixed_capital_qt', 'test_wizard_visual_qt'),
    'coin_overrides': ('test_constructor_coin_policy_atomicity', 'test_veles_coin_transaction_integration'),
    'remove_coin_guard': ('test_wizard_configuration',),
    'sorting': ('test_wizard_visual_qt', 'test_native_dashboard_workflow_qt'),
    'active_config': ('test_constructor_effective_policy_review_complete', 'test_native_dashboard_workflow_qt'),
    'monitor_snapshot': ('test_monitor_async_architecture', 'test_w1_automatic_report'),
    'transfer_full': ('test_transfer', 'test_paper_transfer_recovery'),
    'transfer_clean': ('test_transfer', 'test_transfer_command_boundary'),
    'delete_guard': ('test_control_plane_state_transitions',),
    'veles_rsi': ('test_veles_rsi_continuity', 'test_veles_startup_range'),
    'veles_candles': ('test_veles_rsi_continuity', 'test_live_history_freshness'),
    'veles_transition': ('test_veles_rsi_continuity', 'test_veles_behaviour'),
    'veles_indicators': ('test_strategies', 'test_strategy_dispatch'),
    'veles_dca': ('test_veles_behaviour', 'test_twentieth_position_risk_recovery'),
    'veles_exits': ('test_veles_behaviour', 'test_paper_complete_recovery_acceptance'),
    'liquidity_risk': ('test_health_liquidity', 'test_shared_risk'),
    'poly_markets': ('test_poly_market_integrity', 'test_poly_contract_seconds'),
    'poly_settlement': ('test_paper_complete_recovery_acceptance', 'test_poly_market_integrity'),
    'poly_limits': ('test_poly_market_integrity', 'test_strategies'),
    'pump_both': ('test_strategies', 'test_r617_paper_integrity_matrix'),
    'pump_exit': ('test_strategies', 'test_r617_paper_integrity_matrix'),
    'grid_levels': ('test_grid_preview_model', 'test_strategies'),
    'grid_risk': ('test_shared_risk', 'test_r617_paper_integrity_matrix'),
    'other_live': ('test_strategies', 'test_strategy_dispatch'),
    'stop_restart': ('test_process', 'test_native_dashboard_workflow_qt'),
    'running_identity': ('test_process', 'test_runtime_paths_contract'),
    'after_deals': ('test_process', 'test_control_plane_state_transitions'),
    'early_stop': ('test_process', 'test_control_plane_state_transitions'),
    'pending_grid': ('test_strategies', 'test_position_journal_recovery'),
    'forced_worker': ('test_process', 'test_position_journal_recovery'),
    'fleet_isolation': ('test_market_fleet_recovery_e2e', 'test_portfolio_recovery_integration'),
    'fleet_monitor': ('test_monitor_async_architecture', 'test_market_fleet_recovery_e2e'),
    'outage': ('test_bybit_ws_outage_margin_recovery', 'test_data_blocked_scanner_recovery'),
    'accounting': ('test_margin_finance', 'test_paper_complete_recovery_acceptance'),
}


def _junit_groups(path):
    root = ElementTree.parse(path).getroot()
    groups = {}
    seen = set()
    for case in root.iter('testcase'):
        module = (case.get('classname') or '').rsplit('.', 1)[-1]
        name = case.get('name') or ''
        key = (module, name)
        if not module or not name or key in seen:
            raise ValueError('Invalid or duplicate JUnit case identity')
        seen.add(key)
        counts = groups.setdefault(module, Counter())
        if case.find('failure') is not None or case.find('error') is not None:
            counts['failed'] += 1
        elif case.find('skipped') is not None:
            counts['skipped'] += 1
        else:
            counts['passed'] += 1
    if not seen:
        raise ValueError('Empty JUnit report')
    return groups


def _snapshot_summary(output, captures):
    summaries = []
    for capture in captures:
        name = capture.get('file', '')
        if not name or Path(name).name != name:
            continue
        path = output / name
        if path.is_symlink() or not path.is_file():
            continue
        try:
            with zipfile.ZipFile(path) as archive:
                member = archive.getinfo('current_bots_report/paper_bots/collection.json')
                if member.file_size > 2_000_000:
                    raise ValueError('Oversized bot collection')
                collection = json.loads(archive.read(member))
                bots = collection['bots']
                if not isinstance(bots, list):
                    raise ValueError('Invalid bot collection')
                summaries.append({'file': name, 'bots': len(bots),
                                  'open_positions': sum(int(b.get('open_positions', 0)) for b in bots),
                                  'running_workers': sum(b.get('active_process_confirmed') is True for b in bots),
                                  'ledger_statuses': [b.get('ledger') for b in bots],
                                  'execution_freshness': [b.get('execution_freshness', {}).get('status') for b in bots]})
        except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError, zipfile.BadZipFile):
            summaries.append({'file': name, 'error': 'Snapshot could not be verified'})
    return summaries


def build_report(output, checks, expected_manifest, captures):
    """Read matching receipts only; failures or missing evidence fail closed."""
    output = Path(output)
    automated = output / 'automated_qa' / 'automated'
    groups, problems = {}, []
    try:
        from tools.verify_windows import native_test_evidence_problems, verify_complete_test_inventory
        report = json.loads((automated / 'windows_report.json').read_text(encoding='utf-8'))
        if (not expected_manifest or report.get('status') != 'AUTOMATED_CHECKS_PASS'
                or report.get('source_integrity', {}).get('manifest_sha256') != expected_manifest
                or report.get('source_integrity_final', {}).get('manifest_sha256') != expected_manifest):
            raise ValueError('Windows receipt did not pass for this source')
        junit = automated / 'tests.xml'
        inventory = automated / 'collected_tests.json'
        if junit.is_symlink() or inventory.is_symlink() or junit.stat().st_size > 40_000_000:
            raise ValueError('Invalid automated receipt files')
        verify_complete_test_inventory(junit, inventory)
        problems = native_test_evidence_problems(junit)
        if problems:
            raise ValueError('Windows test evidence incomplete')
        groups = _junit_groups(junit)
        if any(counts.get('failed', 0) for counts in groups.values()):
            raise ValueError('Windows JUnit contains failed tests')
    except (OSError, ValueError, TypeError, KeyError, ElementTree.ParseError) as exc:
        problems.append(str(exc))
        groups = {}
    rows = []
    for key, title in checks:
        modules = RELATED.get(key, ())
        evidence = [{'module': module, **dict(groups.get(module, {}))} for module in modules]
        if any(row.get('failed', 0) for row in evidence):
            status = 'RELATED_TESTS_FAILED'
        elif modules and all(row.get('passed', 0) and not row.get('skipped', 0) for row in evidence):
            status = 'RELATED_TESTS_PASSED'
        else:
            status = 'RELATED_TESTS_NOT_CONFIRMED'
        rows.append({'id': key, 'title': title, 'automated_result': status,
                     'related_test_groups': evidence,
                     'live_acceptance': 'NOT_CONFIRMED'})
    return {'schema': 1, 'source_manifest_sha256': expected_manifest,
            'meaning': 'Related automated tests do not by themselves prove each complete live Windows PAPER criterion.',
            'criteria': rows, 'snapshots': _snapshot_summary(output, captures),
            'receipt_errors': problems, 'release_ready': False}


def render_report(report):
    labels = {'RELATED_TESTS_PASSED': 'связанные автотесты прошли',
              'RELATED_TESTS_FAILED': 'ошибка связанного автотеста',
              'RELATED_TESTS_NOT_CONFIRMED': 'автотесты не подтверждены'}
    lines = [
        'АВТОМАТИЧЕСКИЙ ОТЧЁТ ПРИЁМКИ BCC',
        'Связанные автотесты не подтверждают весь живой сценарий на Windows.',
        'Живая приёмка и выпуск: НЕ ПОДТВЕРЖДЕНЫ.',
        'SHA-256 манифеста: ' + str(report.get('source_manifest_sha256') or 'нет'),
        'Публичный Bybit WebSocket (стакан и mark-price): ' +
        str(report.get('public_bybit_ws', {}).get('status', 'НЕ ПРОВЕРЕН')),
        'Один публичный Bybit VELES PAPER-цикл: ' +
        str(report.get('public_paper_cycle', {}).get('status', 'НЕ ПРОВЕРЕН')),
        '',
    ]
    for index, row in enumerate(report['criteria'], 1):
        lines.append(f"{index}. {row['title']}: {labels.get(row['automated_result'], 'нет данных')}")
        for evidence in row['related_test_groups']:
            lines.append('   ' + evidence['module'] + ': прошло ' + str(evidence.get('passed', 0)) +
                         ', ошибок ' + str(evidence.get('failed', 0)) +
                         ', пропущено ' + str(evidence.get('skipped', 0)))
        lines.append('   Полный живой сценарий: НЕ ПОДТВЕРЖДЁН.')
    lines.extend(['', 'СНИМКИ QA-СЕССИИ'])
    for snap in report['snapshots']:
        if snap.get('error'):
            lines.append(snap['file'] + ': ошибка чтения')
        else:
            lines.append(f"{snap['file']}: ботов {snap['bots']}, работающих {snap['running_workers']}, "
                         f"открытых позиций {snap['open_positions']}; учёт {snap['ledger_statuses']}; "
                         f"свежесть исполнений {snap['execution_freshness']}")
    if report['receipt_errors']:
        lines.extend(['', 'ОШИБКИ ЧТЕНИЯ АВТОПРОВЕРКИ'] + [str(x) for x in report['receipt_errors']])
    return '\n'.join(lines) + '\n'
