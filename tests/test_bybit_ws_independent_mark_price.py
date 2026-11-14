"""Real WS markPrice is independent of executable bid/ask and HTTP replay.

The Bybit ticker stream may deliver a mark-only delta; these tests use the
actual production Hub SQLite publisher/consumer and prohibit HTTP fallback.
"""
import time

import pytest

from bcc.market import PublicData, DataUnavailable
from bcc.market_hub import MarketDataHub
from bcc.ws_quotes import BybitQuoteStream, StreamUnavailable


TICKER = "https://api.bybit.com/v5/market/tickers"
PARAMS = {"category": "linear", "symbol": "BTCUSDT"}


class Clock:
    def __init__(self):
        self.wall = time.time()
        self.mono = time.monotonic()


def feed(stream, clock, generation, kind, **fields):
    return stream.ingest({
        "type": kind,
        "topic": "tickers.BTCUSDT",
        "ts": int(clock.wall * 1000),
        "data": {"symbol": "BTCUSDT", **fields},
    }, generation=generation)


def test_mark_only_delta_refreshes_real_exchange_mark_but_not_bid_ask():
    c = Clock()
    stream = BybitQuoteStream(
        max_age=2, wall=lambda: c.wall, monotonic=lambda: c.mono
    )
    stream.subscribe("BTCUSDT")
    generation = stream.connect()
    assert feed(stream, c, generation, "snapshot",
                bid1Price="100", ask1Price="101", markPrice="100.4")
    assert stream.quote("BTCUSDT")[:2] == (100, 101)
    assert stream.mark("BTCUSDT")[0] == 100.4

    c.wall += 1.5
    c.mono += 1.5
    assert feed(stream, c, generation, "delta", markPrice="100.7")
    assert stream.mark("BTCUSDT")[0] == 100.7
    c.wall += .6
    c.mono += .6
    with pytest.raises(StreamUnavailable):
        stream.quote("BTCUSDT")
    assert stream.mark("BTCUSDT")[0] == 100.7
    stream.disconnect()
    with pytest.raises(StreamUnavailable):
        stream.mark("BTCUSDT")
    new_generation = stream.connect()
    assert new_generation != generation
    assert not feed(stream, c, generation, "delta", markPrice="999")
    with pytest.raises(StreamUnavailable):
        stream.mark("BTCUSDT")


def test_verified_ws_mark_only_hub_snapshot_reaches_risk_reader_without_rest(tmp_path, monkeypatch):
    c = Clock()
    stream = BybitQuoteStream(
        max_age=2, wall=lambda: c.wall, monotonic=lambda: c.mono
    )
    stream.subscribe("BTCUSDT")
    generation = stream.connect()
    producer = PublicData(quote_stream=stream)
    path = tmp_path / "market_cache.sqlite"
    hub = MarketDataHub(path, source=producer)
    try:
        hub.registry.subscribe(TICKER, PARAMS)
        assert hub.acquire_leadership()
        assert feed(stream, c, generation, "snapshot",
                    bid1Price="100", ask1Price="101", markPrice="100.4")
        assert hub.publish_ws_event("tickers", "BTCUSDT")
        monkeypatch.setenv("BCC_PUBLIC_CACHE", str(path))
        monkeypatch.setenv("BCC_MARKET_HUB_MODE", "client")
        monkeypatch.setenv("BCC_BYBIT_WS_QUOTES", "1")
        reader = PublicData()
        try:
            def forbidden(*args, **kwargs):
                raise AssertionError("Bybit REST must not replace the WS mark")
            monkeypatch.setattr(reader.session, "get", forbidden)
            observed = reader._ws_position_mark("BTCUSDT")
            assert observed["bid"] == observed["ask"] == 100.4
            assert observed["at"] == pytest.approx(c.wall, abs=.003)

            # A valid mark-only event may overwrite the ticker cache entry
            # without manufacturing bid1/ask1 observations.
            c.wall += .01
            c.mono += .01
            assert feed(stream, c, generation, "delta", markPrice="100.7")
            assert hub.publish_ws_event("tickers", "BTCUSDT")
            updated = reader._ws_position_mark("BTCUSDT")
            assert updated["bid"] == updated["ask"] == 100.7
            # Calling the generic executable quote reader is *not* authorized
            # by a mark-only record.
            with pytest.raises(DataUnavailable, match="WebSocket"):
                reader.get(TICKER, PARAMS)
        finally:
            reader.close()
    finally:
        hub.close()
        producer.close()


def test_fast_position_book_does_not_invent_risk_mark(monkeypatch):
    source = PublicData()
    monkeypatch.setenv("BCC_MARKET_HUB_MODE", "client")
    monkeypatch.setenv("BCC_BYBIT_WS_QUOTES", "1")
    def denied(symbol):
        raise DataUnavailable("Ticker WebSocket disconnected")
    monkeypatch.setattr(source, "_ws_position_mark", denied)
    monkeypatch.setattr("bcc.market.clock.ensure", lambda: None)
    monkeypatch.setattr("bcc.market.check_book_time", lambda *a, **k: None)
    now = time.time()
    def get(url, params=None, ttl=0):
        if url.endswith("/instruments-info"):
            return {"result": {"list": [{
                "symbol": "BTCUSDT", "status": "Trading",
                "contractType": "LinearPerpetual", "quoteCoin": "USDT",
                "lotSizeFilter": {
                    "qtyStep": ".001", "minOrderQty": ".001",
                    "minNotionalValue": "5", "maxMktOrderQty": "100"},
                "priceFilter": {"tickSize": ".01"},
            }]}}
        if url.endswith("/orderbook"):
            return {"time": int(now * 1000), "result": {
                "s": "BTCUSDT", "ts": int(now * 1000),
                "b": [["99.99", "10"], ["99.98", "10"]],
                "a": [["100.01", "10"], ["100.02", "10"]],
                "u": 1, "seq": 2,
            }}
        raise AssertionError(url)
    monkeypatch.setattr(source, "get", get)
    try:
        book = source.orderbook("BTCUSDT", position_only=True)
        assert book["margin_marks"] == {}
        assert "disconnected" in book["margin_error"]
        assert book["bids"] and book["asks"]
    finally:
        source.close()
