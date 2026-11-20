"""Fresh portfolio observations must not leave stale risk-data diagnostics.

All accounts, prices and exchange metadata here are synthetic and isolated.
Risk evaluation and entry/exit guards remain the production implementations.
"""
import time
from copy import deepcopy

from bcc.config import defaults, range_section
from bcc.market_clock import observed_quote
from bcc.margin import margin_status
from bcc.runtime import _apply_observations, process_tick
from bcc.scheduler import MarketTick
from bcc.storage import Repository, Store
from bcc.strategies import Veles
from bcc.trading import enter, plan
from test_core import attach, book
from test_margin_finance import use_mark


def account(tmp_path, symbols, mode='CROSS'):
    cfg = defaults('VELES')
    cfg['name'] = 'Synthetic diagnostic recovery'
    cfg['symbols'] = symbols + ['SCANUSDT']
    cfg['sections'] = [range_section(30, 40)]
    cfg['settings']['orders']['count'] = 1
    cfg['settings']['margin_mode'] = mode
    folder = Repository(tmp_path).create(cfg)
    store = Store(folder, 10000)
    section = cfg['sections'][0]['id']
    with store.transaction() as (state, events):
        for symbol in symbols:
            attach(state, symbol)
            use_mark(state, symbol=symbol)
            ok, reason = enter(state, events, symbol + ':' + section, symbol, 'LONG',
                               cfg['settings'], 100, 25,
                               plan(cfg['settings']['orders'], 'LONG', 100), section)
            assert ok, reason
            state['marks'][symbol] = observed_quote(dict(bid=99.99, ask=100.01, at=time.time()))
        for symbol in symbols:
            state['margin_data'][symbol]['quote'] = observed_quote(
                dict(bid=100, ask=100, at=time.time() - 10))
        state.pop('_books', None)
    process_tick(cfg, store, Veles(), MarketTick('SCANUSDT', [], book(), [], {}))
    return cfg, folder


def test_cross_error_clears_only_after_all_independent_marks_recover(tmp_path):
    cfg, folder = account(tmp_path, ['BTCUSDT', 'ETHUSDT'])
    store = Store(folder)
    before = store.read()
    assert all(p.get('margin_data_error') for p in before['positions'].values())
    assert len(store.history()) == 2
    fresh = lambda: observed_quote(dict(bid=100, ask=100, at=time.time()))
    with store.transaction() as (state, events):
        _apply_observations(state, ({'BTCUSDT': fresh()}, {}))
        assert all(p.get('margin_data_error') for p in state['positions'].values())
        assert margin_status(state)
        assert events == []
    with store.transaction() as (state, events):
        _apply_observations(state, ({'ETHUSDT': fresh()}, {}))
        assert margin_status(state) == ''
        assert all(not p.get('margin_data_error') for p in state['positions'].values())
        assert events == []
    reopened = Store(folder)
    assert len(reopened.history()) == 2
    assert len(reopened.read()['positions']) == 2
    reopened.audit()


def test_risk_only_refresh_revalidates_after_fresh_tier(tmp_path):
    cfg, folder = account(tmp_path, ['BTCUSDT'])
    store = Store(folder)
    with store.transaction() as (state, events):
        state['margin_data']['BTCUSDT']['quote'] = observed_quote(
            dict(bid=100, ask=100, at=time.time()))
        state['margin_data']['BTCUSDT']['risk_at'] = time.time() - 301
        tier = dict(state['margin_data']['BTCUSDT'])
        tier['risk_at'] = time.time()
        assert next(iter(state['positions'].values())).get('margin_data_error')
    process_tick(cfg, store, Veles(), MarketTick(
        'BTCUSDT', [], None, [], {}, margin_data=tier, risk_only=True))
    reopened = Store(folder)
    assert margin_status(reopened.read()) == ''
    assert not next(iter(reopened.read()['positions'].values())).get('margin_data_error')
    assert len(reopened.history()) == 1
    reopened.audit()


def test_isolated_positions_revalidate_independently_without_fills(tmp_path):
    _, folder = account(tmp_path, ['BTCUSDT', 'ETHUSDT'], mode='ISOLATED')
    store = Store(folder)
    fresh = observed_quote(dict(bid=100, ask=100, at=time.time()))
    with store.transaction() as (state, events):
        _apply_observations(state, ({'BTCUSDT': fresh}, {}))
        positions = {p['symbol']: p for p in state['positions'].values()}
        assert not positions['BTCUSDT'].get('margin_data_error')
        assert positions['ETHUSDT'].get('margin_data_error')
        assert events == []
    store.audit()


def test_cross_diagnostic_is_not_cleared_while_pending_grid_has_no_mark(tmp_path):
    _, folder = account(tmp_path, ['BTCUSDT'])
    state = Store(folder).read()
    position = next(iter(state['positions'].values()))
    pending = deepcopy(position)
    pending['symbol'] = 'MISSINGUSDT'
    pending['orders'] = [{'weight': 1.0, 'price': 100.0}]
    state['pending']['MISSINGUSDT:grid'] = pending
    fresh = observed_quote(dict(bid=100, ask=100, at=time.time()))
    _apply_observations(state, ({'BTCUSDT': fresh}, {}))
    # Open positions alone are valid; the full CROSS calculation must still
    # reject missing independent risk data for the pending grid.
    assert margin_status(state) == ''
    assert position.get('margin_data_error')


def test_future_observation_cannot_clear_a_recorded_data_failure(tmp_path):
    _, folder = account(tmp_path, ['BTCUSDT'])
    store = Store(folder)
    future = observed_quote(dict(bid=100, ask=100, at=time.time() + 30))
    with store.transaction() as (state, events):
        _apply_observations(state, ({'BTCUSDT': future}, {}))
        assert next(iter(state['positions'].values())).get('margin_data_error')
        assert margin_status(state)
        assert events == []
    store.audit()
