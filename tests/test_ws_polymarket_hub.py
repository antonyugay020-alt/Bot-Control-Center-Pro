"""Polymarket market WS -> shared Hub -> PAPER adapter, no live network."""
import time

from bcc.market import PublicData, PolymarketData, DataUnavailable
from bcc.market_hub import MarketDataHub, HubRegistry


import pytest


@pytest.fixture(autouse=True)
def isolated_stream_clock(monkeypatch):
    """A prior test's process-wide clock reference cannot skew local WS
    publication timestamps produced by an offline test stream."""
    from bcc.market_clock import MarketClock
    monkeypatch.setattr("bcc.market_clock.clock", MarketClock())


URL = "https://clob.polymarket.com/book"
PARAMS = {"token_id": "123"}


class FakeMarketStream:
    def __init__(self, fail=False):
        self.fail = fail
        self.subscriptions = []

    def subscribe(self, assets):
        if isinstance(assets, str):
            assets = [assets]
        self.subscriptions.extend(assets)

    def get_book(self, asset):
        from bcc.ws_quotes import StreamUnavailable
        if self.fail:
            raise StreamUnavailable("disconnected")
        assert asset == "123"
        return {
            "asset_id": asset, "timestamp": str(int(time.time() * 1000)),
            "bids": [{"price": "0.45", "size": "10"}],
            "asks": [{"price": "0.47", "size": "20"}],
        }


def test_ws_event_published_to_shared_cache_without_http(tmp_path):
    stream = FakeMarketStream()
    source = PublicData(polymarket_stream=stream)
    hub = MarketDataHub(tmp_path / "clob.sqlite", source=source)
    try:
        hub.registry.subscribe(URL, PARAMS)
        hub.registry.subscribe("https://gamma-api.polymarket.com/markets",
                               {"slug": "btc-updown-5m-1234567890"})
        assert hub.acquire_leadership()
        hub.subscribe_active_websockets()
        assert stream.subscriptions == ["123"]
        assert hub.publish_ws_event("polybook", "123")
        snapshot = source.shared.read_fresh(URL, PARAMS, "clob.polymarket.com")
        assert snapshot["_bcc_ws_polybook"] is True
        assert snapshot["asks"][0]["price"] == "0.47"
        with source.shared.db() as db:
            assert db.execute(
                "SELECT count FROM request_budget WHERE host='clob.polymarket.com'"
            ).fetchone() is None
        remaining = hub.registry.due(10, skip_ws_routes=hub._websocket_queue_exclusions())
        assert len(remaining) == 1
        assert remaining[0][1] == "https://gamma-api.polymarket.com/markets"
    finally:
        hub.close()


def test_closed_or_absent_ws_book_rejected_from_paper_path():
    source = PolymarketData(polymarket_stream=FakeMarketStream(fail=True))
    try:
        try:
            source.get(URL, PARAMS)
        except DataUnavailable:
            pass
        else:
            raise AssertionError("CLOB WS unexpectedly fell back to REST")
    finally:
        source.close()


def test_market_hub_rest_book_kept_when_no_ws(tmp_path):
    registry = HubRegistry(tmp_path / "clob.sqlite")
    registry.subscribe(URL, PARAMS)
    assert registry.due(10)[0][1] == URL
