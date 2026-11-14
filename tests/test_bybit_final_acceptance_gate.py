"""A short healthy book sample cannot certify sustained independent mark flow."""
from copy import deepcopy

import pytest

from tools.bybit_ws_live_acceptance import MIN_ROUNDS, SYMBOLS, validate_report
from tools import verify_windows


def report():
    integrity = {'status': 'PASS', 'manifest_sha256': 'a' * 64}
    return {
        'status': 'PUBLIC_WS_HUB_CLIENT_PASS', 'require_mark': True,
        'real_orders': False, 'isolated_hub_retired': True,
        'source_integrity': integrity, 'source_integrity_final': deepcopy(integrity),
        'requested_rounds': MIN_ROUNDS, 'steady_state_max_seconds': .1,
        'rounds': [[dict(symbol=symbol, status='PASS', mark_price_verified=True,
                         mark_age_seconds=.2, age_upper_seconds=.1,
                         received_in_seconds=.1) for symbol in SYMBOLS]
                   for _ in range(MIN_ROUNDS)],
    }


def test_source_bound_sustained_mark_inventory_is_required():
    assert not validate_report(report(), 'a' * 64)
    assert validate_report(report(), 'b' * 64)


@pytest.mark.parametrize('defect', [
    'short', 'duplicate', 'mark_missing', 'stale_mark', 'stale_book',
    'latency', 'nan', 'cleanup', 'resealed', 'status', 'real_orders',
])
def test_false_or_incomplete_live_evidence_cannot_pass(defect):
    value = report()
    row = value['rounds'][-1][-1]
    if defect == 'short':
        value['rounds'] = value['rounds'][:3]
        value['requested_rounds'] = 3
    elif defect == 'duplicate':
        row['symbol'] = 'ADAUSDT'
    elif defect == 'mark_missing':
        row['mark_price_verified'] = False
    elif defect == 'stale_mark':
        row['mark_age_seconds'] = 4
    elif defect == 'stale_book':
        row['age_upper_seconds'] = 4
    elif defect == 'latency':
        row['received_in_seconds'] = 1.6
        value['steady_state_max_seconds'] = 1.6
    elif defect == 'nan':
        row['mark_age_seconds'] = float('nan')
    elif defect == 'cleanup':
        value['isolated_hub_retired'] = False
    elif defect == 'resealed':
        value['source_integrity_final']['manifest_sha256'] = 'b' * 64
    elif defect == 'status':
        value['status'] = 'PUBLIC_WS_DELIVERY_INCOMPLETE'
    elif defect == 'real_orders':
        value['real_orders'] = True
    assert validate_report(value, 'a' * 64)


def test_native_bybit_gate_rejects_absent_live_transport_evidence(tmp_path):
    problems = verify_windows.validate_required_evidence(tmp_path, venue='bybit')
    assert any('Bybit WS evidence unavailable' in problem for problem in problems)
