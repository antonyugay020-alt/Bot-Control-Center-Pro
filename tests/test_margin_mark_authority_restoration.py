"""Margin mark-price is authoritative for collateral; depth is for execution.

Both are separately fail-closed. Synthetic PAPER data, no live requests.
"""
import math
import time
from copy import deepcopy

import pytest

from bcc.margin import MODEL
from bcc.market_clock import observed_quote
from bcc.runtime import process_tick
from bcc.scheduler import MarketTick
from bcc.storage import Store
from bcc.trading import available_margin, enter, plan
from test_core import attach, config
from test_margin_finance import opened, use_mark


def test_stale_unrelated_execution_depth_does_not_invalidate_fresh_margin_marks():
    state, _, _ = opened(mode='CROSS')
    state['marks']['BTCUSDT'] = observed_quote(dict(
        bid=1, ask=2, at=time.time()-30))
    assert state['margin_model'] == MODEL
    assert available_margin(state) > 0
    assert state['margin_data']['BTCUSDT']['quote']['at'] > time.time() - 3


@pytest.mark.parametrize('fault', ['no_public_margin', 'stale_public_mark', 'expired_risk_tiers'])
def test_margin_safety_still_fails_closed_without_authoritative_price(fault):
    state, _, _ = opened(mode='CROSS')
    state['marks']['BTCUSDT'] = observed_quote(dict(
        bid=99, ask=100, at=time.time()))
    if fault == 'no_public_margin':
        del state['margin_data']['BTCUSDT']
    elif fault == 'stale_public_mark':
        state['margin_data']['BTCUSDT']['quote']['at'] -= 15
        state['margin_data']['BTCUSDT']['quote']['source_age_high'] += 15
    else:
        state['margin_data']['BTCUSDT']['risk_at'] -= 400
    assert available_margin(state) == -math.inf


def test_failed_executable_book_preserves_valid_separate_margin_proof(tmp_path):
    store = Store(tmp_path, 500)
    cfg = config()
    cfg['settings']['orders']['count'] = 1
    with store.transaction() as (state, events):
        attach(state)
        ok, why = enter(state, events, 'BTCUSDT:first', 'BTCUSDT',
                        'LONG', cfg['settings'], 100, 25,
                        plan(cfg['settings']['orders'], 'LONG', 100), 'first')
        assert ok, why
        use_mark(state)
    class Engine:
        def process(self, *args):
            raise AssertionError('Missing executable depth reached the trading engine')
    process_tick(cfg, store, Engine(), MarketTick(
        'BTCUSDT', [], None, [], {}, quote_error='public depth not available'))
    state = store.read()
    assert 'BTCUSDT' not in state['marks']
    assert 'BTCUSDT' in state['margin_data']
    assert available_margin(state) > 0
    store.audit()


def test_current_tick_admission_uses_valid_portfolio_mark_not_other_depth(tmp_path):
    from bcc.scheduler import MarketTick
    from test_core import book
    cfg = config()
    cfg['symbols'] = ['BTCUSDT', 'ETHUSDT']
    cfg['settings']['orders']['count'] = 1
    store = Store(tmp_path, 500)
    with store.transaction() as (state, events):
        attach(state)
        ok, why = enter(state, events, 'BTCUSDT:first', 'BTCUSDT',
                        'LONG', cfg['settings'], 100, 25,
                        plan(cfg['settings']['orders'], 'LONG', 100), 'first')
        assert ok, why
        use_mark(state)
        state['marks']['BTCUSDT'] = observed_quote(dict(
            bid=99.9, ask=100.1, at=time.time()-15))
    captured = []
    class Spy:
        def process(self, cfg, state, events, symbol, bars, bid, ask, now):
            captured.append((symbol, state.get('_entry_blocked')))
    process_tick(cfg, store, Spy(), MarketTick(
        'ETHUSDT', [{'t': int(time.time()*1000)}], book(101), [], {}))
    assert captured == [('ETHUSDT', None)], captured
    store.audit()


def test_scheduler_reports_public_margin_freshness_separately_from_depth():
    from bcc.scheduler import MarketScheduler
    state, _, _ = opened(mode='CROSS')
    state['marks']['BTCUSDT'] = observed_quote(dict(
        bid=99, ask=100, at=time.time()-15))
    class Fake:
        def position_orderbook(self, symbol):
            return {'at': time.time(), 'bids': [[99, 1]], 'asks': [[100, 1]]}
        def position_candles(self, symbol, required):
            return []
        def candles(self, symbol, required):
            return []
        def funding(self, symbol):
            return []
    scheduler = MarketScheduler(Fake(), {'symbols': ['BTCUSDT']},
                                lambda *_: 100, workers=2)
    try:
        scheduler.collect(state, timeout=.02)
        assert scheduler.last_pressure['unquoted_priority'] == 1
        assert scheduler.last_pressure['unmarked_priority'] == 0
        assert not scheduler.last_pressure['entry_admission_paused']
        state['margin_data']['BTCUSDT']['quote']['at'] -= 15
        state['margin_data']['BTCUSDT']['quote']['source_age_high'] += 15
        scheduler.collect(state, timeout=.02)
        assert scheduler.last_pressure['unmarked_priority'] == 1
        assert scheduler.last_pressure['entry_admission_paused']
    finally:
        scheduler.close()
