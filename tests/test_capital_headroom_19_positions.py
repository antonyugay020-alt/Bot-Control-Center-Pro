"""Actual collateral exhaustion is independent of any fixed position count.

Reproduces the report's 500k deposit / five-percent-per-section setup with
synthetic execution depth and prices. No private account data or HTTP.
"""
import math
from bcc.config import defaults, effective, validate
from bcc.storage import initial_state
from bcc.trading import enter, plan, reserved, available_margin
from test_core import book
from test_margin_finance import use_mark


def _enter_positions(pct, want):
    cfg = defaults('VELES')
    cfg['name'] = 'Budget headroom reproduction'
    cfg['capital'] = 500_000
    cfg['symbols'] = [f'COIN{i}USDT' for i in range(45)]
    cfg['settings']['allocation_pct'] = pct
    cfg = validate(cfg)
    state = initial_state(cfg['capital'])
    events = []
    result = None
    for i, symbol in enumerate(cfg['symbols'][:want]):
        settings = effective(cfg, symbol)
        state['_books'] = {symbol: book(price=100, size=100_000)}
        use_mark(state, mark=100, symbol=symbol)
        margin = cfg['capital'] * settings['allocation_pct'] / 100
        result = enter(state, events, f'{symbol}:one', symbol, 'LONG', settings,
                       100, margin, plan(settings['orders'], 'LONG', 100), 'one')
        if not result[0]:
            break
    return state, result, events


def test_five_percent_of_500k_exhausts_at_nineteen_not_a_fixed_slot_limit():
    state, result, events = _enter_positions(5.0, 20)
    assert len(state['positions']) == 19
    assert not result[0]
    assert 'Недостаточно маржи' in result[1]
    assert 'свободно' in result[1] and 'требуется' in result[1]
    assert 475_000 < reserved(state) < 480_000
    assert 0 < available_margin(state) < 25_000
    assert len([event for event in events if event['action'] == 'OPEN']) == 19


def test_smaller_one_percent_coin_allocation_exceeds_nineteen_safely():
    state, result, events = _enter_positions(1.0, 35)
    assert result[0], result[1]
    assert len(state['positions']) == 35
    assert available_margin(state) > 0
    assert len([event for event in events if event['action'] == 'OPEN']) == 35
