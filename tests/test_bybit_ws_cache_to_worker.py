"""Offline end-to-end Bybit WS event -> fenced Hub SQLite -> client snapshot.

No public network and no real orders: data origin, subscriber lease, producer
leadership and client provenance are real production code paths.
"""
import time

from bcc.market import PublicData
from bcc.market_hub import MarketDataHub


BOOK_URL = "https://api.bybit.com/v5/market/orderbook"
BOOK_PARAMS = {"category": "linear", "symbol": "BTCUSDT", "limit": 50}


class VerifiedStream:
    def get_orderbook(self, symbol):
        assert symbol == "BTCUSDT"
        return {
            "ts": time.time(),
            "bids": ((99.99, 10.0), (99.98, 8.0)),
            "asks": ((100.01, 10.0), (100.02, 8.0)),
            "u": 3, "seq": 7,
        }


def test_bounded_ws_book_reaches_client_with_verified_provenance_no_rest(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("BCC_PUBLIC_CACHE", raising=False)
    monkeypatch.delenv("BCC_MARKET_HUB_MODE", raising=False)
    monkeypatch.delenv("BCC_BYBIT_WS_BOOKS", raising=False)
    path = tmp_path / "market_cache.sqlite"
    producer = PublicData(orderbook_stream=VerifiedStream())
    hub = MarketDataHub(path, source=producer)
    try:
        hub.registry.subscribe(BOOK_URL, BOOK_PARAMS, lifetime=60)
        assert hub.acquire_leadership()
        assert hub.publish_ws_event("depth50", "BTCUSDT")
        # Enter client mode ONLY after the isolated producer published.
        monkeypatch.setenv("BCC_PUBLIC_CACHE", str(path))
        monkeypatch.setenv("BCC_MARKET_HUB_MODE", "client")
        monkeypatch.setenv("BCC_BYBIT_WS_BOOKS", "1")
        client = PublicData()
        try:
            def no_network(*args, **kwargs):
                raise AssertionError("WS-backed executable book must not use HTTP")
            monkeypatch.setattr(client.session, "get", no_network)
            value = client.get(BOOK_URL, BOOK_PARAMS)
            assert value["_bcc_ws_book"] is True
            assert value["result"]["s"] == "BTCUSDT"
            assert float(value["result"]["a"][0][0]) > float(value["result"]["b"][0][0])
            assert abs(value["result"]["ts"] / 1000 - time.time()) < 3
        finally:
            client.close()
    finally:
        hub.close()
