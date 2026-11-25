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
            # The executable quote stream has stopped while the mark stream
            # still receives its own current exchange event.
            c.mono += 3.0
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



def test_worker_rejects_old_exchange_mark_with_newer_quote_time(monkeypatch):
    # A quote-only delta must never launder the original exchange mark time.
    source = PublicData()
    current = time.time()
    monkeypatch.setattr(source, "get", lambda *a, **kw: {
        "_bcc_ws_quote": True,
        "_bcc_ws_mark": True,
        "_bcc_ws_mark_at": int((current - 12) * 1000),
        "time": int(current * 1000),
        "result": {"list": [{
            "symbol": "BTCUSDT",
            "bid1Price": "102", "ask1Price": "103",
            "markPrice": "100.5",
        }]},
    })
    try:
        with pytest.raises(DataUnavailable, match="stale"):
            source._ws_position_mark("BTCUSDT")
    finally:
        source.close()

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
            return {"retCode": 0, "time": int(now * 1000), "result": {
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


@pytest.mark.parametrize("warmup", [False, True])
def test_quote_only_cache_waits_for_actual_mark_publication(tmp_path, monkeypatch, warmup):
    """Real SQLite Hub -> mark consumer: first partial row is not terminal."""
    c = Clock()
    stream = BybitQuoteStream(max_age=2, wall=lambda: c.wall, monotonic=lambda: c.mono)
    stream.subscribe("BTCUSDT")
    generation = stream.connect()
    producer = PublicData(quote_stream=stream)
    path = tmp_path / "market_cache.sqlite"
    hub = MarketDataHub(path, source=producer)
    reader = None
    try:
        hub.registry.subscribe(TICKER, PARAMS)
        assert hub.acquire_leadership()
        assert feed(stream, c, generation, "snapshot", bid1Price="100", ask1Price="101")
        assert hub.publish_ws_event("tickers", "BTCUSDT")
        monkeypatch.setenv("BCC_PUBLIC_CACHE", str(path))
        monkeypatch.setenv("BCC_MARKET_HUB_MODE", "client")
        monkeypatch.setenv("BCC_BYBIT_WS_QUOTES", "1")
        reader = PublicData()
        monkeypatch.setattr(reader.session, "get", lambda *a, **k: pytest.fail("REST forbidden"))
        first = reader.shared.read_fresh(TICKER, PARAMS, "api.bybit.com")
        assert first["_bcc_ws_quote"] is True and first["_bcc_ws_mark"] is not True
        calls = []
        def deliver_on_wait(delay):
            calls.append(delay)
            assert len(calls) == 1, "consumer did not accept genuine mark publication"
            c.wall = time.time()
            c.mono = time.monotonic()
            assert feed(stream, c, generation, "delta", markPrice="100.7")
            assert hub.publish_ws_event("tickers", "BTCUSDT")
            return False
        monkeypatch.setattr(reader.stop_event, "wait", deliver_on_wait)
        reader._thread.mark_subscription_warmup = warmup
        observed = reader._ws_position_mark("BTCUSDT")
        assert calls == [.05]
        assert observed["bid"] == observed["ask"] == 100.7
        assert observed["at"] == int(c.wall * 1000) / 1000
        assert not reader._thread.mark_intent
    finally:
        if reader is not None:
            reader.close()
        hub.close()
        producer.close()


@pytest.mark.parametrize("outcome", ["timeout", "cancel", "denied", "unverified"])
def test_partial_mark_wait_remains_bounded_and_fail_closed(tmp_path, monkeypatch, outcome):
    from bcc.data_cache import ProviderDenied
    from bcc.market import SourceAccessDenied
    from bcc import market as market_module
    from types import SimpleNamespace
    from bcc.market_hub import HubRegistry

    monkeypatch.setenv("BCC_MARKET_HUB_MODE", "client")
    monkeypatch.setenv("BCC_BYBIT_WS_QUOTES", "1")
    monkeypatch.delenv("BCC_PUBLIC_CACHE", raising=False)
    reader = PublicData()
    now = [1000.]
    reads = []
    waits = []
    class PartialCache:
        path = tmp_path / "isolated.sqlite"
        def read_fresh(self, *args):
            reads.append(args)
            if outcome == "denied" and len(reads) > 1:
                raise ProviderDenied("api.bybit.com", 403)
            return dict(retCode=0, _bcc_ws_quote=outcome != "unverified",
                        _bcc_ws_mark=False)
        def close(self):
            pass
    class Registry:
        def subscribe(self, url, params, **kwargs):
            return HubRegistry.identity(url, params)[0]
    reader.shared = PartialCache()
    reader._hub_registry = Registry()
    monkeypatch.setattr(market_module, "time", SimpleNamespace(
        monotonic=lambda: now[0], time=time.time, sleep=time.sleep))
    def wait(delay):
        waits.append(delay)
        now[0] += delay
        if outcome == "cancel":
            reader.stop_event.set()
        return reader.stop_event.is_set()
    monkeypatch.setattr(reader.stop_event, "wait", wait)
    monkeypatch.setattr(reader.session, "get", lambda *a, **k: pytest.fail("REST forbidden"))
    try:
        with pytest.raises(SourceAccessDenied if outcome == "denied" else DataUnavailable):
            reader._ws_position_mark("BTCUSDT")
        if outcome == "timeout":
            assert .8 <= now[0] - 1000. <= .86
            assert 16 <= len(waits) <= 17
        elif outcome in ("cancel", "denied"):
            assert waits == [.05]
        else:
            assert waits == []
        assert not reader._thread.mark_intent
    finally:
        reader.close()
