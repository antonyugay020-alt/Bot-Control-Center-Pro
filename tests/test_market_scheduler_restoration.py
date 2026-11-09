"""Exposure/discovery independence on a large configured market universe.

Disposable fake public sources only. No orders or real HTTP requests.
"""
import threading
import time
from bcc.scheduler import MarketScheduler


def _state(universe, positions=20):
    return {
        'positions': {f'p{i}': {'symbol': universe[i], 'opened': 1} for i in range(positions)},
        'pending': {}, 'funding_archive': {}, 'marks': {},
        'strategy': {'draining': False},
    }


def test_unmarked_exposure_does_not_reduce_market_scan_to_one_slot():
    universe = [f'COIN{i}USDT' for i in range(822)]
    reads = []
    lock = threading.Lock()
    class Source:
        def position_orderbook(self, symbol):
            return {'at': time.time(), 'bids': [[10, 1]], 'asks': [[11, 1]]}
        def candles(self, symbol, required):
            with lock:
                reads.append(symbol)
            time.sleep(.15)
            return [{'t': 1}]
        def orderbook(self, symbol):
            return {'at': time.time(), 'bids': [[10, 1]], 'asks': [[11, 1]]}
        def funding(self, symbol):
            return []
    scheduler = MarketScheduler(Source(), {'symbols': universe}, lambda *_: 100,
                                workers=8, period=0.05)
    try:
        scheduler.collect(_state(universe), timeout=.005)
        scanner_jobs = [s for s in scheduler.jobs if s not in set(universe[:20])]
        assert len(scanner_jobs) >= 4, (scanner_jobs, scheduler.last_pressure)
        assert scheduler.last_pressure['entry_admission_paused'] is True
    finally:
        scheduler.close()


def test_stale_position_marks_do_not_starve_802_new_coins_of_822():
    all_symbols = [f'COIN{i}USDT' for i in range(822)]
    seen = set()
    quotes = set()
    lock = threading.Lock()
    class Source:
        def position_orderbook(self, symbol):
            with lock:
                quotes.add(symbol)
            return {'at': time.time(), 'bids': [[10, 1]], 'asks': [[11, 1]]}
        def candles(self, symbol, required):
            with lock:
                seen.add(symbol)
            time.sleep(.0005)
            return []
        def orderbook(self, symbol):
            return {'at': time.time(), 'bids': [[10, 1]], 'asks': [[11, 1]]}
        def funding(self, symbol):
            return []
    scheduler = MarketScheduler(Source(), {'symbols': all_symbols}, lambda *_: 100,
                                workers=8, period=.01)
    state = _state(all_symbols)
    try:
        deadline = time.monotonic() + 12
        scanner = set(all_symbols[20:])
        while time.monotonic() < deadline and not scanner.issubset(seen):
            scheduler.collect(state, timeout=.003)
        assert scanner.issubset(seen), f'Only {len(seen & scanner)} of 802 scanned'
        assert set(all_symbols[:20]).issubset(quotes), f'Only {len(quotes)} priority quotes'
        assert scheduler.last_pressure['entry_admission_paused'] is True
    finally:
        scheduler.close()
