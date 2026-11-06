"""One public-GET trust boundary for direct transport and durable Hub admission.

No live HTTP, account keys, PAPER writes or trading operations are performed.
"""

import pytest

from bcc.market import PublicData
from bcc.market_hub import HubRegistry


PUBLIC_URLS = [
    "https://api.bybit.com/v5/market/tickers",
    "https://api.bybit.com/v5/market/orderbook",
    "https://api.bybit.com/v5/market/funding/history",
    "https://gamma-api.polymarket.com/markets",
    "https://clob.polymarket.com/book",
    "https://clob.polymarket.com/clob-markets/0x1234",
    "https://data-api.polymarket.com/v2/tokens",
    "https://api.binance.com/api/v3/time",
    "https://api.binance.com/api/v3/klines",
    "https://api.binance.com/api/v3/ticker/price",
]

UNTRUSTED_URLS = [
    "http://api.bybit.com/v5/market/tickers",
    "https://api.bybit.com:8443/v5/market/tickers",
    "https://api.bybit.com:443/v5/market/tickers",
    "https://user:password@api.bybit.com/v5/market/tickers",
    "https://api.bybit.com/v5/market/tickers?api_key=secret",
    "https://api.bybit.com/v5/market/tickers#token",
    "https://api.bybit.com/v5/order/realtime",
    "https://api.binance.com/api/v3/account",
    "https://clob.polymarket.com/orders",
    "https://data-api.polymarket.com/private",
    "https://gamma-api.polymarket.com/markets/../../private",
    "https://api.bybit.com/v5/market/%2e%2e/order",
    "https://api.bybit.com/v5/market//orderbook",
    "https://api.bybit.com/v5/market/tickers\\private",
    "https://api.bybit.com.evil.example/v5/market/tickers",
    "https://api.bybit.com/v5/market/tickers\nX-Injected: yes",
]


@pytest.mark.parametrize("url", PUBLIC_URLS)
def test_allowed_public_routes_have_same_host_in_both_paths(tmp_path, url):
    host = PublicData.validated_public_host(url)
    assert host in PublicData.HOSTS
    registry = HubRegistry(tmp_path / "hub.sqlite")
    key = registry.subscribe(url, {"symbol": "BTCUSDT"})
    assert isinstance(key, str) and len(key) == 64
    with registry._connect() as db:
        assert db.execute(
            "SELECT url FROM hub_subscriptions WHERE k=?", (key,)
        ).fetchone() == (url,)


@pytest.mark.parametrize("url", UNTRUSTED_URLS)
def test_untrusted_route_cannot_be_persisted_or_fetched(tmp_path, monkeypatch, url):
    monkeypatch.delenv("BCC_PUBLIC_CACHE", raising=False)
    monkeypatch.delenv("BCC_MARKET_HUB_MODE", raising=False)
    registry = HubRegistry(tmp_path / "hub.sqlite")
    with pytest.raises(ValueError):
        registry.subscribe(url, {"symbol": "BTCUSDT"})
    with registry._connect() as db:
        assert db.execute("SELECT count(*) FROM hub_subscriptions").fetchone()[0] == 0

    class NoNetwork:
        def get(self, *args, **kwargs):
            raise AssertionError("untrusted route reached network")

        def close(self):
            pass

    source = PublicData()
    source._thread.session = NoNetwork()
    try:
        with pytest.raises(ValueError):
            source.get(url, {"symbol": "BTCUSDT"})
        assert source.cache == {}
    finally:
        source.close()


def test_ordinary_public_query_arguments_are_not_embedded_in_hub_url(tmp_path):
    registry = HubRegistry(tmp_path / "hub.sqlite")
    url = "https://clob.polymarket.com/book"
    key = registry.subscribe(url, {"token_id": "0xabc"})
    with registry._connect() as db:
        row = db.execute(
            "SELECT url,params FROM hub_subscriptions WHERE k=?", (key,)
        ).fetchone()
    assert row == (url, '{"token_id":"0xabc"}')


def test_legacy_hub_row_with_private_url_is_quarantined_before_dispatch(tmp_path):
    registry = HubRegistry(tmp_path / "hub.sqlite")
    private_url = "https://api.bybit.com/v5/order/realtime"
    key, params = registry.identity(private_url, {"symbol": "BTCUSDT"})
    with registry._connect() as db:
        db.execute(
            "INSERT INTO hub_subscriptions(k,url,params,active_until,next_due) "
            "VALUES(?,?,?,?,0)",
            (key, private_url, params, __import__("time").time() + 300),
        )
    assert registry.due() == []
    with registry._connect() as db:
        error, retry_at = db.execute(
            "SELECT last_error,next_due FROM hub_subscriptions WHERE k=?", (key,)
        ).fetchone()
    assert error == "Invalid persisted subscription params"
    assert retry_at > __import__("time").time() + 30
