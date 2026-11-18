"""PAPER exposure beyond 19 is gated by evidence, not a position ceiling."""
import time

from bcc.config import defaults, range_section
from bcc.market_clock import observed_quote
from bcc.runtime import process_tick
from bcc.scheduler import MarketTick
from bcc.storage import Repository, Store
from bcc.trading import enter, plan
from test_core import attach, book
from test_margin_finance import use_mark


def test_twentieth_entry_waits_for_stale_cross_mark_and_recovers(tmp_path):
    cfg = defaults('VELES')
    cfg['name'] = 'Synthetic twenty positions'
    symbols = [f'COIN{i}USDT' for i in range(20)]
    cfg['symbols'] = symbols
    cfg['sections'] = [range_section(30, 40)]
    cfg['settings']['orders']['count'] = 1
    folder = Repository(tmp_path).create(cfg)
    store = Store(folder, 10000)
    section = cfg['sections'][0]['id']
    with store.transaction() as (state, events):
        for symbol in symbols[:19]:
            attach(state, symbol)
            use_mark(state, symbol=symbol)
            ok, reason = enter(state, events, symbol+':'+section, symbol, 'LONG',
                               cfg['settings'], 100, 25,
                               plan(cfg['settings']['orders'], 'LONG', 100), section)
            assert ok, reason
            state['marks'][symbol] = observed_quote(dict(bid=99.99, ask=100.01, at=time.time()))
        use_mark(state, symbol=symbols[19])
        state['margin_data'][symbols[0]]['quote'] = observed_quote(
            dict(bid=100, ask=100, at=time.time()-6))
        state.pop('_books', None)

    class AttemptEntry:
        def __init__(self):
            self.results = []
        def process(self, cfg, state, events, symbol, bars, bid, ask, now):
            self.results.append(enter(state, events, symbol+':'+section, symbol, 'LONG',
                cfg['settings'], ask, 25, plan(cfg['settings']['orders'], 'LONG', ask), section))

    engine = AttemptEntry()
    tick = lambda: MarketTick(symbols[19], [dict(t=1, open=100, high=101, low=99, close=100, volume=1)], book(), [], {})
    process_tick(cfg, store, engine, tick())
    assert len(Store(folder).read()['positions']) == 19
    assert engine.results[-1][0] is False and 'mark-price' in engine.results[-1][1]
    fresh = observed_quote(dict(bid=100, ask=100, at=time.time()))
    process_tick(cfg, store, engine, tick(), observations=({symbols[0]: fresh}, {}))
    reopened = Store(folder)
    assert engine.results[-1][0] is True
    assert len(reopened.read()['positions']) == 20
    assert len(reopened.history()) == 20
    reopened.audit()
