"""WS publication and HTTP budgets share policy leases, not rate counters."""
import pytest

from bcc.data_cache import PublicCache

HOST = "api.bybit.com"
URL = "https://api.bybit.com/v5/market/tickers"


def test_websocket_publication_does_not_consume_http_budget(tmp_path):
    cache = PublicCache(tmp_path / "ws_public.sqlite")
    try:
        for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
            key, owner, _ = cache.begin(
                URL, {"category": "linear", "symbol": symbol},
                HOST, websocket=True)
            assert owner
            assert cache.finish(
                key, owner, HOST,
                {"_bcc_ws_quote": True, "symbol": symbol},
                ttl=.35)
        with cache.db() as db:
            assert db.execute(
                "SELECT count FROM request_budget WHERE host=?", (HOST,)
            ).fetchone() is None
        key, owner, _ = cache.begin(
            "https://api.bybit.com/v5/market/orderbook",
            {"category": "linear", "symbol": "BTCUSDT"},
            HOST)
        assert owner
        with cache.db() as db:
            assert db.execute(
                "SELECT count FROM request_budget WHERE host=?", (HOST,)
            ).fetchone()[0] == 1
    finally:
        cache.close()


def test_depth50_publication_is_not_an_http_request(tmp_path):
    cache = PublicCache(tmp_path / "depth_public.sqlite")
    try:
        url = "https://api.bybit.com/v5/market/orderbook"
        params = {"category": "linear", "symbol": "BTCUSDT", "limit": 50}
        key, owner, _ = cache.begin(url, params, HOST, websocket=True)
        assert owner
        assert cache.finish(
            key, owner, HOST, {"_bcc_ws_book": True, "result": {"s": "BTCUSDT"}},
            ttl=.2)
        with cache.db() as db:
            assert db.execute(
                "SELECT count FROM request_budget WHERE host=?", (HOST,)
            ).fetchone() is None
    finally:
        cache.close()


@pytest.mark.parametrize("url,params,host", [
    ("https://api.bybit.com/v5/market/kline",
     {"category": "linear", "symbol": "BTCUSDT", "interval": "1"}, HOST),
    (URL, {"category": "inverse", "symbol": "BTCUSDT"}, HOST),
    (URL, {"category": "linear", "symbol": "BTCUSDT"}, "api.binance.com"),
    (URL, {"category": "linear", "symbol": "BTCUSDT", "secret": "x"}, HOST),
])
def test_websocket_budget_exemption_is_route_bound(tmp_path, url, params, host):
    cache = PublicCache(tmp_path / "ws_public.sqlite")
    try:
        with pytest.raises(ValueError):
            cache.begin(url, params, host, websocket=True)
    finally:
        cache.close()
