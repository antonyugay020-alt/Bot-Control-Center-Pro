"""Minute WS tail integration must preserve REST historical backfill routes."""
import time

from bcc.market import PublicData
from bcc.market_hub import HubRegistry, MarketDataHub


URL = "https://api.bybit.com/v5/market/kline"
TAIL = {"category": "linear", "symbol": "BTCUSDT",
        "interval": "1", "limit": 10}
HISTORY = {"category": "linear", "symbol": "BTCUSDT",
           "interval": "1", "limit": 500}


class FakeCandle:
    def __init__(self):
        stamp = int(time.time() * 1000)
        self.message_ms = stamp
        self.start_ms = stamp // 60000 * 60000
        self.open = 100.
        self.high = 101.
        self.low = 99.
        self.close = 100.5
        self.volume = 2.


class FakeKlineStream:
    def __init__(self):
        self.subscriptions = []

    def subscribe(self, symbols):
        self.subscriptions.extend(symbols)

    def get_candle(self, symbol):
        assert symbol == "BTCUSDT"
        return FakeCandle()


def test_kline_event_publishes_tail_without_using_rest(tmp_path):
    source = PublicData(kline_stream=FakeKlineStream())
    hub = MarketDataHub(tmp_path / "hub.sqlite", source=source)
    try:
        hub.registry.subscribe(URL, TAIL)
        hub.registry.subscribe(URL, HISTORY)
        assert hub.acquire_leadership()
        hub.subscribe_active_websockets()
        assert source._kline_stream.subscriptions == ["BTCUSDT"]
        assert hub.publish_ws_event("kline1", "BTCUSDT")
        data = source.shared.read_fresh(URL, TAIL, "api.bybit.com")
        assert data["_bcc_ws_kline"] is True
        assert data["result"]["list"][0][4] == "100.5"
        assert source.shared.read_fresh(URL, HISTORY, "api.bybit.com") is None
        due = hub.registry.due(10, skip_ws_routes=hub._websocket_queue_exclusions())
        assert [(url, params) for _, url, params in due] == [(URL, HISTORY)]
    finally:
        hub.close()


def test_kline_range_requests_never_excluded(tmp_path):
    registry = HubRegistry(tmp_path / "hub.sqlite")
    historical = {"category": "linear", "symbol": "BTCUSDT",
                  "interval": "1", "limit": 10,
                  "end": 1763000000000}
    registry.subscribe(URL, TAIL)
    registry.subscribe(URL, historical)
    due = registry.due(10, skip_ws_routes=(URL,))
    assert len(due) == 1
    assert due[0][2] == historical
