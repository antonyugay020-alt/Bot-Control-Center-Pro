"""Full synthetic scanner universe through scheduler, VELES and SQLite.

There are no network requests or orders here. The signal is disabled; outage,
held-position pressure and actual WS transport require separate checks.
"""
import time
from types import SimpleNamespace

import bcc.strategies as strategies
from bcc.config import defaults, range_section
from bcc.runtime import process_tick
from bcc.scheduler import MarketScheduler
from bcc.storage import Repository, Store
from bcc.strategies import Veles


def test_824_configured_scanner_symbols_reach_worker_and_durable_store(tmp_path, monkeypatch):
    class Source:
        def candles(self, symbol, required):
            return [dict(t=(int(time.time()) // 60 - 1) * 60000,
                         open=100, high=101, low=99, close=100, volume=1)]

        def orderbook(self, symbol):
            return dict(at=time.time(), bids=[[99.99, 100]], asks=[[100.01, 100]],
                        qty_step=.001, min_qty=.001, min_notional=.01, tick_size=.01)

        def funding(self, symbol):
            return []

    cfg = defaults('VELES')
    cfg['name'] = 'Isolated synthetic scan'
    cfg['symbols'] = [f'COIN{i}USDT' for i in range(824)]
    cfg['sections'] = [range_section(30, 40)]
    monkeypatch.setattr(strategies, 'signal', lambda state, key, spec, bars, now:
                        SimpleNamespace(matched=False, available=True, observations=[]))
    folder = Repository(tmp_path).create(cfg)
    store = Store(folder, 10000)
    scheduler = MarketScheduler(Source(), cfg, lambda *_: 100, workers=8, period=3600)
    seen = set()
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and len(seen) < 824:
            for symbol, tick, error in scheduler.collect(store.read(), timeout=.01):
                assert error is None, (symbol, error)
                process_tick(cfg, store, Veles(), tick)
                seen.add(symbol)
        assert seen == set(cfg['symbols'])
        assert not store.read()['positions']
        assert not store.history()
        store.audit()
    finally:
        scheduler.close()
