"""No dashboard may present stale/malformed PAPER marks as current PnL."""
import math
import time

import pytest

from bcc.position_report import position_mark_pnl


def poly():
    p = dict(kind='POLYMARKET', symbol='BTC', token='token',
             side='UP', shares=10, cost=5)
    state = dict(marks={'token': dict(at=time.time(), bid=.55, ask=.56)})
    return state, p


def test_fresh_valid_quote_is_displayed_consistently():
    state, p = poly()
    assert position_mark_pnl(state, p) == pytest.approx(.5)


@pytest.mark.parametrize('modify', [
    {'bid': float('nan')}, {'ask': float('inf')},
    {'bid': .7, 'ask': .6}, {'bid': -1}, {'ask': 1.2},
    {'bid': '0.5'}, {'ask': None}, {'at': math.nan},
    {'at': time.time() + 300}, {'at': time.time() - 300},
])
def test_bad_quote_never_yields_numerical_pnl(modify):
    state, p = poly()
    state['marks']['token'].update(modify)
    assert position_mark_pnl(state, p) is None


def test_missing_quote_and_overflow_are_unknown():
    state, p = poly()
    state['marks'].clear()
    assert position_mark_pnl(state, p) is None
    state['marks']['token'] = dict(at=time.time(), bid=.8, ask=.9)
    p['shares'] = math.inf
    p['cost'] = 1e308
    assert position_mark_pnl(state, p) is None
