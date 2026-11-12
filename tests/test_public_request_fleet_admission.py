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



@pytest.mark.parametrize("url,params", [
    (URL, {"category": "linear", "secretKey": "never-store-this"}),
    (URL, {"category": "linear", "key": "never-store-this"}),
    (URL, {"category": "linear", "token_id": "0xabc"}),
    ("https://clob.polymarket.com/book", {"symbol": "BTCUSDT"}),
    ("https://api.binance.com/api/v3/time", {"symbol": "BTCUSDT"}),
    (URL, {"symbol": "BTCUSDT?api_key=never-store-this"}),
    (URL, {"symbol": "BTCUSDT/../../"}),
    (URL, {"category": "inverse"}),
    (URL, {"category": "linear", "symbol": 12345}),
    ("https://gamma-api.polymarket.com/markets", {"limit": "100"}),
    ("https://gamma-api.polymarket.com/markets", {"offset": -1}),
    ("https://gamma-api.polymarket.com/markets",
     {"end_date_min": "never-store-this"}),
    ("https://clob.polymarket.com/clob-markets/0x1234", {"token_id": "0xabc"}),
])
def test_route_bound_query_schema_blocks_unapproved_fields_and_values(
    tmp_path, monkeypatch, url, params
):
    monkeypatch.delenv("BCC_PUBLIC_CACHE", raising=False)
    monkeypatch.delenv("BCC_MARKET_HUB_MODE", raising=False)
    registry = HubRegistry(tmp_path / "hub.sqlite")
    with pytest.raises(ValueError) as error:
        registry.subscribe(url, params)
    assert "never-store-this" not in str(error.value)
    source = PublicData()
    try:
        with pytest.raises(ValueError) as error:
            source.get(url, params)
        assert "never-store-this" not in str(error.value)
        with registry._connect() as db:
            assert db.execute("SELECT COUNT(*) FROM hub_subscriptions").fetchone()[0] == 0
    finally:
        source.close()


@pytest.mark.parametrize("url,params", [
    ("https://api.bybit.com/v5/market/instruments-info",
     {"category": "linear", "limit": 1000, "cursor": "eyJwYWdlIjoyfQ=="}),
    ("https://api.bybit.com/v5/market/risk-limit",
     {"category": "linear", "symbol": "BTCUSDT", "cursor": "abc+/="}),
    ("https://api.bybit.com/v5/market/kline",
     {"category": "linear", "symbol": "BTCUSDT", "interval": "1",
      "end": 1700000000000, "limit": 1000}),
    ("https://api.bybit.com/v5/market/mark-price-kline",
     {"category": "linear", "symbol": "BTCUSDT", "interval": "1",
      "start": 1700000000000, "limit": 1}),
    ("https://gamma-api.polymarket.com/markets",
     {"active": "true", "closed": "false", "limit": 100, "offset": 0,
      "end_date_min": "2026-11-07T00:00:00+00:00",
      "end_date_max": "2026-11-07T01:00:00+00:00"}),
    ("https://api.binance.com/api/v3/klines",
     {"symbol": "ETHUSDT", "interval": "1m",
      "startTime": 1700000000000, "limit": 1}),
])
def test_real_adapter_query_shapes_remain_valid(url, params):
    assert PublicData.validated_public_request(url, params) in PublicData.HOSTS


def test_dynamic_clob_metadata_id_rejects_path_punctuation():
    with pytest.raises(ValueError):
        PublicData.validated_public_host(
            "https://clob.polymarket.com/clob-markets/0x1234;secret"
        )
