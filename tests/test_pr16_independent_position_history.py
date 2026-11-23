"""PR16 regression: book transport failure must not suppress candle refresh.

Non-executable history may be fetched without a fresh depth50 snapshot.
The financial owner still must reject entries without a fresh book/mark/risk.
"""
import threading
import time

from bcc.market import DataUnavailable
from bcc.market_clock import observed_quote
from bcc.scheduler import MarketScheduler


def test_existing_position_history_is_requested_even_when_book_absent():
    asked = threading.Event()

    class FakeSource:
        def set_position_symbols(self, symbols):
            pass

        def position_orderbook(self, symbol):
            raise DataUnavailable('Independent WS book is deliberately missing')

        def position_candles(self, symbol, required):
            asked.set()
            return []

        def funding(self, symbol):
            return []

    symbol = 'BTCUSDT'
    state = {
        'positions': {'held:independent': {'symbol': symbol, 'opened': time.time()}},
        'pending': {}, 'funding_archive': {},
        'marks': {symbol: observed_quote({'bid': 99.9, 'ask': 100.1,
                                           'at': time.time()})},
        'margin_data': {},
    }
    scheduler = MarketScheduler(FakeSource(), {'symbols': [symbol]},
                                lambda *_: 100, workers=2, period=2)
    try:
        for _ in range(20):
            scheduler.collect(state, timeout=.02)
            if asked.wait(.01):
                break
        assert asked.is_set(), 'Indicator history must not wait for a WS book'
    finally:
        scheduler.close()


def test_first_quote_handoff_is_not_starved_by_98_already_served_positions():
    """With unlimited exposure IDs, first executable-book delivery has priority.

    The oracle checks quote worker admission only, NOT permission to trade;
    the owner still validates book, mark and tier age in process_tick.
    """
    from threading import Lock
    called = []
    guard = Lock()
    symbols = [f'HELD{i}USDT' for i in range(100)]

    class FakeSource:
        def set_position_symbols(self, symbols):
            pass

        def position_orderbook(self, symbol):
            with guard:
                called.append(symbol)
            return {'at': time.time(), 'bids': [[99.9, 10]],
                    'asks': [[100.1, 10]]}

        def position_candles(self, symbol, required):
            return []

        def funding(self, symbol):
            return []

        def risk_maintenance_enabled(self):
            return False

    state = {
        'positions': {f'p{i}': {'symbol': s, 'opened': time.time()}
                      for i, s in enumerate(symbols)},
        'pending': {}, 'funding_archive': {},
        'marks': {s: observed_quote({'bid': 99.9, 'ask': 100.1,
                                     'at': time.time()}) for s in symbols},
        'margin_data': {},
    }
    scheduler = MarketScheduler(FakeSource(), {'symbols': symbols},
                                lambda *_: 100, workers=8, period=2)
    scheduler._quoted_once.update(symbols[:-2])
    scheduler.quote_cursor = 0
    try:
        scheduler.collect(state, timeout=.1)
        with guard:
            delivered = set(called)
        assert symbols[-1] in delivered and symbols[-2] in delivered, (
            'Unserved exposed positions must get first quote slots')
    finally:
        scheduler.close()
