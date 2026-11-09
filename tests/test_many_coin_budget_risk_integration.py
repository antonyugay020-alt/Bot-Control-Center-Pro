"""End-to-end PAPER accounting beyond the observed nineteen-position plateau.

Only deterministic verified synthetic exchange data; no live orders or HTTP.
"""
import math
import time

from bcc.config import defaults, effective, validate
from bcc.coin_allocation import remaining_coin_budget
from bcc.market_clock import observed_quote
from bcc.portfolio_marks import commit_portfolio_marks
from bcc.storage import Store
from bcc.trading import enter, plan, available_margin
from test_margin_finance import use_mark
from test_core import book


def test_31_independent_coin_budgets_enter_and_survive_full_ledger_audit(tmp_path):
    cfg = defaults('VELES')
    cfg['name'] = 'Thirty-one symbols'
    cfg['capital'] = 5000
    cfg['symbols'] = [f'COIN{i}USDT' for i in range(35)]
    cfg['settings'].update(leverage=3, allocation_mode='coin_fixed',
                           allocation_usdt=25, execution_policy='ioc', slippage_pct=0)
    cfg['settings']['orders']['count'] = 1
    cfg = validate(cfg)
    store = Store(tmp_path, cfg['capital'])
    with store.transaction() as (state, events):
        for i, symbol in enumerate(cfg['symbols'][:31]):
            settings = effective(cfg, symbol)
            state.setdefault('_books', {})[symbol] = book(price=100, size=10000)
            use_mark(state, 100, symbol=symbol)
            amount = remaining_coin_budget(cfg, state, symbol)
            assert amount == 25
            ok, reason = enter(state, events, f'{symbol}:one', symbol,
                               'LONG', settings, 100, amount,
                               plan(settings['orders'], 'LONG', 100), 'one')
            assert ok, (i, symbol, reason)
            assert remaining_coin_budget(cfg, state, symbol) == 0
        assert len(state['positions']) == 31
        assert available_margin(state) > 0
        state.pop('_books', None)
    store.audit()
    assert len(store.read()['positions']) == 31
    assert len([event for event in store.history() if event['action'] == 'OPEN']) == 31

    # One stale collateral quote must still deny new entry. A single complete
    # Bybit mark-price batch may restore all thirty-one valuations atomically.
    with store.transaction(include_charts=False) as (state, _events):
        state['margin_data'][cfg['symbols'][0]]['quote'] = observed_quote(
            dict(bid=100, ask=100, at=time.time()-20))
    assert available_margin(store.read()) == -math.inf
    packet = {symbol: observed_quote(dict(bid=100, ask=100, at=time.time()))
              for symbol in cfg['symbols'][:31]}
    assert commit_portfolio_marks(store, packet) == 31
    assert available_margin(store.read()) > 0
    store.audit()
    assert len(store.history()) == 31
