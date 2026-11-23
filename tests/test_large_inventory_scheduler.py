"""No artificial symbol/position ceiling in admission or executable read prioritization.

All upstream observations here are synthetic; this is a scheduler scalability
contract, not proof that an exchange can service every selected subscription.
"""
import time
import pytest

from bcc.config import defaults, range_section, validate
from bcc.scheduler import MarketScheduler
from bcc.market_clock import observed_quote


class SyntheticSource:
    def __init__(self):
        self.positions = []
        self.history_calls = 0
        self.quote_calls = 0
        self.quoted_symbols = []

    def set_position_symbols(self, symbols):
        self.positions = list(symbols)

    def risk_maintenance_enabled(self):
        return False

    def position_orderbook(self, symbol):
        self.quote_calls += 1
        self.quoted_symbols.append(symbol)
        return {'at': time.time(), 'bids': [[99., 1.]], 'asks': [[101., 1.]]}

    def orderbook(self, symbol):
        return {'at': time.time(), 'bids': [[99., 1.]], 'asks': [[101., 1.]]}

    def position_candles(self, symbol, required):
        self.history_calls += 1
        return []

    candles = position_candles

    def funding(self, symbol):
        return []


def state_for(symbols, positions, distinct_position_symbols):
    opened = time.time() - 90
    positions = {
        f'position-{i}': {'symbol': symbols[i % distinct_position_symbols], 'opened': opened - i}
        for i in range(positions)
    }
    marks = {p['symbol']: observed_quote({'bid': 100., 'ask': 100., 'at': time.time()})
             for p in positions.values()}
    return {'positions': positions, 'pending': {}, 'funding_archive': {},
            'marks': marks, 'margin_data': {}}


@pytest.mark.parametrize('symbol_count,position_count,distinct_position_symbols', [(831, 20, 20), (6000, 1000, 250), (12000, 5000, 1000)])
def test_large_inventory_admits_all_positions_without_silent_truncation(symbol_count,position_count,distinct_position_symbols):
    symbols = [f'COIN{i}USDT' for i in range(symbol_count)]
    cfg = defaults('VELES')
    cfg['name'] = 'Scale acceptance'
    cfg['sections'] = [range_section(30, 40)]
    cfg['symbols'] = symbols
    validate(cfg)
    source = SyntheticSource()
    state = state_for(symbols, position_count, distinct_position_symbols)
    scheduler = MarketScheduler(source, cfg, lambda *_: 5, workers=8, period=0)
    delivered = set()
    started = time.monotonic()
    try:
        # Simulate repeated owner visits; the first priority quotes must get
        # an admission turn even when other scanner work also exists.
        for _ in range(24):
            for symbol, tick, error in scheduler.collect(state, timeout=0):
                assert error is None
                if tick is not None and tick.book is not None:
                    delivered.add(symbol)
        assert len(state['positions']) == position_count
        assert len(source.positions) == distinct_position_symbols
        assert scheduler.last_pressure['configured_symbols'] == symbol_count
        assert scheduler.last_pressure['priority_symbols'] == distinct_position_symbols
        assert source.quote_calls > 0
        assert set(source.quoted_symbols) <= set(symbols[:distinct_position_symbols])
        assert len(set(source.quoted_symbols)) >= min(5, distinct_position_symbols)
        # A generous regression guard against accidentally reintroducing
        # quadratic owner-side scans; no hard real-time guarantee is implied.
        assert time.monotonic() - started < 35
    finally:
        scheduler.close()
