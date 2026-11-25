"""No dashboard may present stale/malformed PAPER marks as current PnL."""
import math

import pytest

from bcc.position_report import position_mark_pnl


NOW = 1_800_000_000.0


def poly(now=NOW):
    p = dict(kind='POLYMARKET', symbol='BTC', token='token',
             side='UP', shares=10, cost=5)
    state = dict(marks={'token': dict(at=now, bid=.55, ask=.56)})
    return state, p


def test_fresh_valid_quote_is_displayed_consistently():
    state, p = poly()
    assert position_mark_pnl(state, p, now=NOW) == pytest.approx(.5)


@pytest.mark.parametrize('modify', [
    {'bid': float('nan')}, {'ask': float('inf')},
    {'bid': .7, 'ask': .6}, {'bid': -1}, {'ask': 1.2},
    {'bid': '0.5'}, {'ask': None}, {'at': math.nan},
])
def test_bad_quote_never_yields_numerical_pnl(modify):
    state, p = poly()
    state['marks']['token'].update(modify)
    assert position_mark_pnl(state, p, now=NOW) is None


def test_missing_quote_and_overflow_are_unknown():
    state, p = poly()
    state['marks'].clear()
    assert position_mark_pnl(state, p, now=NOW) is None
    state['marks']['token'] = dict(at=NOW, bid=.8, ask=.9)
    p['shares'] = math.inf
    p['cost'] = 1e308
    assert position_mark_pnl(state, p, now=NOW) is None


@pytest.mark.parametrize('offset', [300, -300, .501, -15.001])
@pytest.mark.parametrize('now', [NOW, NOW + 900])
def test_out_of_window_quote_is_unknown_independent_of_collection_time(now, offset):
    state, p = poly(now)
    state['marks']['token']['at'] = now + offset
    assert position_mark_pnl(state, p, now=now) is None


@pytest.mark.parametrize('offset', [.5, 0, -15])
def test_quote_at_exact_display_age_boundary_is_valid(offset):
    state, p = poly()
    state['marks']['token']['at'] = NOW + offset
    assert position_mark_pnl(state, p, now=NOW) == pytest.approx(.5)
