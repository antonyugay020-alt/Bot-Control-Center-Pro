"""Unified public-request trust boundary for workers and durable Hub rows.

PAPER-only tests: no network, trading accounts, credentials or orders.
"""

import time

import pytest

from bcc.market import PublicData
from bcc.market_hub import HubRegistry


URL = "https://api.bybit.com/v5/market/tickers"


@pytest.mark.parametrize("route", [
    "https://api.bybit.com/v5/market/private",
    "https://api.bybit.com/v5/market/order/create",
    "https://api.bybit.com/v5/market/tickers/extra",
])
def test_unknown_bybit_routes_are_rejected_before_hub_or_network(tmp_path, monkeypatch, route):
    monkeypatch.delenv("BCC_PUBLIC_CACHE", raising=False)
    monkeypatch.delenv("BCC_MARKET_HUB_MODE", raising=False)
    registry = HubRegistry(tmp_path / "hub.sqlite")
    with pytest.raises(ValueError):
        registry.subscribe(route, {"symbol": "BTCUSDT"})
    source = PublicData()
    try:
        with pytest.raises(ValueError):
            source.get(route, {"symbol": "BTCUSDT"})
        with registry._connect() as db:
            assert db.execute("SELECT count(*) FROM hub_subscriptions").fetchone()[0] == 0
    finally:
        source.close()


@pytest.mark.parametrize("params", [
    {"api_key": "never-store-this"},
    {"Authorization": "never-store-this"},
    {"client_secret": "never-store-this"},
    {"signature": "never-store-this"},
    {"access_token": "never-store-this"},
    {"symbol": "BTCUSDT\r\nAuthorization: secret"},
    {"symbol": float("nan")},
    {"symbol": ["BTCUSDT"]},
    {"symbol": {"private_key": "secret"}},
    {"x" * 65: "BTCUSDT"},
])
def test_private_or_ambiguous_params_fail_closed_in_both_paths(
    tmp_path, monkeypatch, params
):
    monkeypatch.delenv("BCC_PUBLIC_CACHE", raising=False)
    monkeypatch.delenv("BCC_MARKET_HUB_MODE", raising=False)
    registry = HubRegistry(tmp_path / "hub.sqlite")
    with pytest.raises(ValueError) as error:
        registry.subscribe(URL, params)
    assert "never-store-this" not in str(error.value)
    source = PublicData()
    try:
        with pytest.raises(ValueError) as error:
            source.get(URL, params)
        assert "never-store-this" not in str(error.value)
        assert source.cache == {}
        with registry._connect() as db:
            assert db.execute("SELECT count(*) FROM hub_subscriptions").fetchone()[0] == 0
    finally:
        source.close()


def test_legacy_persisted_sensitive_params_quarantined_before_dispatch(tmp_path):
    registry = HubRegistry(tmp_path / "hub.sqlite")
    params = {"category": "linear", "api_key": "never-store-this"}
    key, raw = registry.identity(URL, params)
    with registry._connect() as db:
        db.execute(
            "INSERT INTO hub_subscriptions(k,url,params,active_until,next_due) "
            "VALUES(?,?,?,?,0)",
            (key, URL, raw, time.time() + 300),
        )
    assert registry.due() == []
    with registry._connect() as db:
        error, retry = db.execute(
            "SELECT last_error,next_due FROM hub_subscriptions WHERE k=?", (key,)
        ).fetchone()
    assert error == "Invalid persisted public subscription"
    assert "never-store-this" not in error
    assert retry > time.time() + 30


@pytest.mark.parametrize("url,params", [
    ("https://api.bybit.com/v5/market/funding/history",
     {"category": "linear", "symbol": "BTCUSDT", "limit": 50}),
    ("https://clob.polymarket.com/book", {"token_id": "0xabc"}),
    ("https://gamma-api.polymarket.com/markets", {"slug": "btc-updown-5m-123"}),
    ("https://api.binance.com/api/v3/time", None),
])
def test_existing_public_market_queries_remain_admitted(tmp_path, url, params):
    assert PublicData.validated_public_request(url, params) in PublicData.HOSTS
    registry = HubRegistry(tmp_path / "hub.sqlite")
    assert len(registry.subscribe(url, params)) == 64
