"""No phantom top-of-book prices may trigger a signal or liquidation."""
from copy import deepcopy
from time import time
import pytest

from bcc.market import DataUnavailable, executable_quote
from bcc.runtime import process_tick
from bcc.scheduler import MarketTick
from bcc.storage import Store
from test_core import book, config


def test_non_executable_top_levels_are_not_used_for_quote():
    snapshot = book(price=100)
    snapshot['bids'].insert(0, [10, 0])
    snapshot['asks'].insert(0, [200, 0])
    bid, ask = executable_quote(snapshot)
    assert bid == 99.99 and ask == 100.01


@pytest.mark.parametrize('mutate', [
    lambda b: b.update(bids=[[100,0]]),
    lambda b: b.update(asks=[[100,0]]),
    lambda b: b.update(bids=[[float('nan'),5]]),
    lambda b: b.update(bids=[[103,5]],asks=[[102,5]]),
])
def test_invalid_or_empty_executable_depth_fails_closed(mutate):
    snapshot = book(price=100)
    mutate(snapshot)
    with pytest.raises(DataUnavailable):
        executable_quote(snapshot)


def test_worker_mark_uses_executable_quote_not_zero_size_spoof(tmp_path):
    store = Store(tmp_path, 500)
    snapshot = book(price=100)
    snapshot['bids'].insert(0, [1,0])
    snapshot['asks'].insert(0, [999,0])
    class Probe:
        def process(self, cfg, state, events, symbol, bars, bid, ask, now):
            assert bid == 99.99 and ask == 100.01
    process_tick(config(), store, Probe(), MarketTick('BTCUSDT', [], snapshot, [], {}))
    mark = store.read(False)['marks']['BTCUSDT']
    assert mark['bid'] == 99.99 and mark['ask'] == 100.01
    store.audit()


def test_actual_bybit_orderbook_does_not_use_zero_depth_quotes(monkeypatch):
    """Transport -> orderbook -> executable marks must agree on nonzero depth."""
    from bcc.market import PublicData
    from bcc import market
    source = PublicData()
    snapshot = book(price=100)
    snapshot['bids'].insert(0, [150, 0])
    snapshot['asks'].insert(0, [50, 0])
    # Both spurious top rows cross, but no units are available there.
    def get(url, params=None, ttl=0):
        return {'result': {'list': [{'symbol': 'BTCUSDT', 'status':'Trading',
            'contractType':'LinearPerpetual', 'quoteCoin':'USDT',
            'lotSizeFilter': {'qtyStep':'0.0001', 'minOrderQty':'0.0001',
                              'minNotionalValue':'5', 'maxMktOrderQty':'100'},
            'priceFilter': {'tickSize':'0.01'}}]}}
    def bybit(route, params):
        assert route == 'orderbook'
        return {'result': {'s': 'BTCUSDT',
                           'b': [[str(p),str(q)] for p,q in snapshot['bids']],
                           'a': [[str(p),str(q)] for p,q in snapshot['asks']],
                           'ts': str(int(time()*1000))}, 'time':int(time()*1000)}
    monkeypatch.setattr(source, 'get', get)
    monkeypatch.setattr(source, 'bybit', bybit)
    monkeypatch.setattr(market.clock, 'ensure', lambda: None)
    monkeypatch.setattr(market, 'check_book_time', lambda *args, **kwargs: None)
    result = source.orderbook('BTCUSDT')
    assert executable_quote(result) == (99.99,100.01)
    source.close()
