"""PAPER exposure beyond 19 is gated by evidence, not a position ceiling."""
import time
from types import SimpleNamespace

import pytest

from bcc.config import defaults, range_section
from bcc.market_clock import observed_quote
from bcc.runtime import process_tick
from bcc.scheduler import MarketTick
from bcc.storage import Repository, Store
from bcc.strategies import Veles
from bcc.trading import enter, plan
from test_core import attach, book
from test_margin_finance import use_mark


@pytest.mark.parametrize('engine_kind', ['direct', 'veles'])
def test_twentieth_entry_waits_for_stale_cross_mark_and_recovers(tmp_path, monkeypatch, engine_kind):
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
            state['positions'][symbol+':'+section]['exit_signal'] = cfg['sections'][0]['exit']
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

    engine = AttemptEntry() if engine_kind == 'direct' else Veles()
    if engine_kind == 'veles':
        # Exercise the actual strategy and runtime while controlling only the
        # indicator result; admission, prices, ledger and risk are real.
        monkeypatch.setattr('bcc.strategies.signal', lambda state, key, spec, bars, now:
                            SimpleNamespace(matched=key.endswith(':entry'),
                                            available=True, observations=[]))
    tick = lambda: MarketTick(symbols[19], [dict(t=(int(time.time())//60-1)*60000,
        open=100, high=101, low=99, close=100, volume=1)], book(), [], {})
    process_tick(cfg, store, engine, tick())
    blocked = Store(folder).read()
    assert len(blocked['positions']) == 19
    if engine_kind == 'direct':
        assert engine.results[-1][0] is False and 'mark-price' in engine.results[-1][1]
    else:
        assert any('mark-price' in row['reason'] for row in blocked['diagnostics'][symbols[19]])
    fresh = observed_quote(dict(bid=100, ask=100, at=time.time()))
    process_tick(cfg, store, engine, tick(), observations=({symbols[0]: fresh}, {}))
    reopened = Store(folder)
    if engine_kind == 'direct':
        assert engine.results[-1][0] is True
    assert len(reopened.read()['positions']) == 20
    assert len(reopened.history()) == 20
    reopened.audit()


def test_veles_dca_waits_for_cross_mark_then_resumes_after_store_reopen(tmp_path, monkeypatch):
    cfg = defaults('VELES')
    cfg['name'] = 'Synthetic DCA recovery'
    cfg['symbols'] = ['HELDUSDT', 'DCAUSDT']
    cfg['sections'] = [range_section(30, 40)]
    cfg['settings']['orders'].update(count=2, coverage_pct=10, first_offset_pct=0)
    folder = Repository(tmp_path).create(cfg)
    store = Store(folder, 10000)
    section = cfg['sections'][0]['id']
    with store.transaction() as (state, events):
        for symbol in cfg['symbols']:
            attach(state, symbol)
            use_mark(state, symbol=symbol)
            ok, reason = enter(state, events, symbol+':'+section, symbol, 'LONG',
                               cfg['settings'], 100, 100,
                               plan(cfg['settings']['orders'], 'LONG', 100), section)
            assert ok, reason
            state['positions'][symbol+':'+section]['exit_signal'] = cfg['sections'][0]['exit']
            state['marks'][symbol] = observed_quote(dict(bid=99.99, ask=100.01, at=time.time()))
        state['margin_data']['HELDUSDT']['quote'] = observed_quote(
            dict(bid=100, ask=100, at=time.time()-6))
        state.pop('_books', None)
    monkeypatch.setattr('bcc.strategies.signal', lambda state, key, spec, bars, now:
                        SimpleNamespace(matched=False, available=True, observations=[]))
    tick = lambda: MarketTick('DCAUSDT', [dict(t=(int(time.time())//60-1)*60000,
        open=80, high=81, low=79, close=80, volume=1)], book(80), [], {})
    position = 'DCAUSDT:'+section
    before = Store(folder).read()['positions'][position]
    process_tick(cfg, Store(folder), Veles(), tick())
    blocked = Store(folder)
    assert blocked.read()['positions'][position]['qty'] == before['qty']
    assert [e['action'] for e in blocked.history()].count('DCA') == 0
    now = time.time()
    marks = {'HELDUSDT': observed_quote(dict(bid=100, ask=100, at=now)),
             'DCAUSDT': observed_quote(dict(bid=80, ask=80, at=now))}
    process_tick(cfg, Store(folder), Veles(), tick(), observations=(marks, {}))
    reopened = Store(folder)
    assert reopened.read()['positions'][position]['qty'] > before['qty']
    assert [e['action'] for e in reopened.history()].count('DCA') == 1
    reopened.audit()
