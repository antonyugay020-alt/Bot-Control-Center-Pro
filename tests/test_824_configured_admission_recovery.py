"""Risk evidence expiry blocks, then real VELES admission resumes.

Synthetic prices and isolated SQLite only. 824 symbols are configured; this
case exercises seven of them, not a full-universe Hub throughput claim.
"""
import time
from types import SimpleNamespace

import pytest

from bcc.config import defaults, range_section
from bcc.market import DataUnavailable
from bcc.market_clock import observed_quote
from bcc.runtime import process_tick
from bcc.scheduler import MarketTick
from bcc.storage import Repository, Store
from bcc.strategies import Veles
from bcc.trading import enter, plan
from test_core import attach, book
from test_margin_finance import use_mark


def test_expired_tier_and_market_failures_block_then_fresh_evidence_resumes(tmp_path, monkeypatch):
    cfg = defaults('VELES')
    cfg['name'] = 'Isolated synthetic 824-symbol admission'
    symbols = [f'COIN{i}USDT' for i in range(824)]
    cfg['symbols'] = symbols
    cfg['sections'] = [range_section(30, 40)]
    cfg['settings']['orders']['count'] = 1
    folder = Repository(tmp_path).create(cfg)
    store = Store(folder, 10000)
    section = cfg['sections'][0]['id']
    held, candidate = symbols[:6], symbols[6]
    with store.transaction() as (state, events):
        for symbol in held:
            attach(state, symbol)
            use_mark(state, symbol=symbol)
            ok, reason = enter(
                state, events, symbol + ':' + section, symbol, 'LONG',
                cfg['settings'], 100, 25,
                plan(cfg['settings']['orders'], 'LONG', 100), section,
            )
            assert ok, reason
            state['positions'][symbol + ':' + section]['exit_signal'] = cfg['sections'][0]['exit']
            state['marks'][symbol] = observed_quote(dict(bid=99.99, ask=100.01, at=time.time()))
        attach(state, candidate)
        use_mark(state, symbol=candidate)
        state['margin_data'][held[0]]['risk_at'] = time.time() - 301
        candidate_margin = dict(state['margin_data'][candidate])
        state.pop('_books', None)

    # Control only the strategy signal; runtime, risk, execution and ledger
    # remain the real implementation. There are no network requests.
    monkeypatch.setattr(
        'bcc.strategies.signal',
        lambda state, key, spec, bars, now: SimpleNamespace(
            matched=key.endswith(':entry'), available=True, observations=[]),
    )

    def held_observations():
        fresh = observed_quote(dict(bid=100, ask=100, at=time.time()))
        return ({s: fresh for s in held}, {s: fresh for s in held})

    def tick():
        candidate_margin['quote'] = observed_quote(dict(bid=100, ask=100, at=time.time()))
        bar = dict(t=(int(time.time()) // 60 - 1) * 60000,
                   open=100, high=101, low=99, close=100, volume=1)
        return MarketTick(candidate, [bar], book(), [], {}, margin_data=candidate_margin)

    process_tick(cfg, store, Veles(), tick(), observations=held_observations())
    blocked = Store(folder).read()
    assert len(blocked['positions']) == 6
    assert any('Устарели публичные ступени риска' in row['reason']
               for row in blocked['diagnostics'][candidate])
    with store.transaction() as (state, events):
        renewed = dict(state['margin_data'][held[0]])
        renewed['risk_at'] = time.time()
    process_tick(cfg, store, Veles(), MarketTick(
        held[0], [], None, [], {}, margin_data=renewed, risk_only=True))

    stale = tick()
    stale.book['at'] = time.time() - 20
    with pytest.raises(DataUnavailable, match='устарел'):
        process_tick(cfg, Store(folder), Veles(), stale)
    assert len(Store(folder).history()) == 6

    missing_history = tick()
    missing_history.history_error = 'synthetic history timeout'
    process_tick(cfg, Store(folder), Veles(), missing_history, observations=held_observations())
    assert len(Store(folder).history()) == 6
    process_tick(cfg, Store(folder), Veles(), tick(), observations=held_observations())

    reopened = Store(folder)
    assert len(reopened.read()['positions']) == 7
    assert len(reopened.history()) == 7
    reopened.audit()
