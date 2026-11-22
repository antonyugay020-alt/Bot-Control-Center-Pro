"""Regression: a dead one-minute WS tail must not strand valid Bybit REST history.

All network and clock calls are replaced with deterministic local sources.
Independent mark/risk protections are deliberately not modified by this fix.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from bcc.market import (
    DataUnavailable, PublicData, SourceAccessDenied, WebSocketKlineUnavailable,
)

KLINE = "https://api.bybit.com/v5/market/kline"
SYMBOL = "BTCUSDT"
NOW = 1800000300.0  # synthetic, minute-aligned time


def rows_ending(last_ms, count=100):
    return [
        [str(last_ms - (count-1-i)*60000), "100", "102",
         "99", "101", "1", "0"]
        for i in range(count)
    ]


def bars_ending(last_ms, count=100):
    return [dict(t=int(row[0]), open=100., high=102., low=99.,
                 close=101., volume=1.)
            for row in rows_ending(last_ms, count)]


def fake_response(last_ms, count):
    return {"retCode": 0, "retMsg": "OK", "time": int(NOW*1000),
            "result": {"list": rows_ending(last_ms, count)}}


@pytest.fixture
def source(monkeypatch):
    monkeypatch.setattr("bcc.market.clock.ensure", lambda: None)
    monkeypatch.setattr("bcc.market.market_now", lambda: NOW)
    instance = PublicData()
    yield instance
    instance.close()


def test_stale_warm_tail_requests_bounded_rest_history_not_ws_only(source, monkeypatch):
    monkeypatch.setenv("BCC_MARKET_HUB_MODE", "client")
    monkeypatch.setenv("BCC_BYBIT_WS_KLINES", "1")
    newest = int(NOW//60)*60000
    source.cache[("bars", SYMBOL)] = (NOW-200, bars_ending(newest-180000))
    calls = []

    def fetch(route, params):
        calls.append(dict(params))
        assert route == "kline"
        assert params["limit"] == 100, "stale warm history cannot request WS-only tail"
        return fake_response(newest, 100)

    monkeypatch.setattr(source, "bybit", fetch)
    recovered = source.candles(SYMBOL, 100)
    assert recovered[-1]["t"] == newest
    assert len(recovered) == 100 and len(calls) == 1
    assert all(y["t"]-x["t"] == 60000 for x,y in zip(recovered, recovered[1:]))


def test_missing_ws_tail_uses_one_explicit_rest_backfill(source, monkeypatch):
    monkeypatch.setenv("BCC_MARKET_HUB_MODE", "client")
    monkeypatch.setenv("BCC_BYBIT_WS_KLINES", "1")
    newest = int(NOW//60)*60000
    source.cache[("bars", SYMBOL)] = (NOW-30, bars_ending(newest-60000))
    calls = []

    def fetch(route, params):
        assert route == "kline"
        calls.append(dict(params))
        if params["limit"] == 10:
            raise WebSocketKlineUnavailable("synthetic WS tail absent")
        assert params["limit"] == 100
        return fake_response(newest, 100)

    monkeypatch.setattr(source, "bybit", fetch)
    recovered = source.candles(SYMBOL, 100)
    assert [c["limit"] for c in calls] == [10, 100]
    assert recovered[-1]["t"] == newest
    assert all(y["t"]-x["t"] == 60000 for x,y in zip(recovered, recovered[1:]))


def test_provider_denial_does_not_trigger_rest_fallback(source, monkeypatch):
    monkeypatch.setenv("BCC_MARKET_HUB_MODE", "client")
    monkeypatch.setenv("BCC_BYBIT_WS_KLINES", "1")
    newest = int(NOW//60)*60000
    source.cache[("bars", SYMBOL)] = (NOW-30, bars_ending(newest-60000))
    calls = []

    def blocked(route, params):
        calls.append(params["limit"])
        raise SourceAccessDenied("api.bybit.com", 451)

    monkeypatch.setattr(source, "bybit", blocked)
    with pytest.raises(SourceAccessDenied):
        source.candles(SYMBOL, 100)
    assert calls == [10]


def test_rate_cooldown_not_classified_as_ws_outage(source, monkeypatch):
    newest = int(NOW//60)*60000
    source.cache[("bars", SYMBOL)] = (NOW-30, bars_ending(newest-60000))
    calls = []

    def rate_block(route, params):
        calls.append(params["limit"])
        raise DataUnavailable("Источник временно ограничил запросы: api.bybit.com")

    monkeypatch.setattr(source, "bybit", rate_block)
    with pytest.raises(DataUnavailable, match="временно ограничил"):
        source.candles(SYMBOL, 100)
    assert calls == [10]


def test_rest_gap_is_not_fabricated_when_history_unavailable(source, monkeypatch):
    newest = int(NOW//60)*60000
    older = bars_ending(newest-180000)
    source.cache[("bars", SYMBOL)] = (NOW-200, older)

    def persist_gap(route, params):
        assert params["limit"] == 100
        # Only a newer candle; no intermediate history received.
        return fake_response(newest, 1)

    monkeypatch.setattr(source, "bybit", persist_gap)
    with pytest.raises(DataUnavailable, match="Разрывы"):
        source.candles(SYMBOL, 100)
    assert source.cache[("bars", SYMBOL)][1][-1]["t"] == older[-1]["t"]


def test_rest_history_stale_cannot_authorize_strategy(source, monkeypatch):
    newest = int(NOW//60)*60000
    source.cache[("bars", SYMBOL)] = (NOW-300, bars_ending(newest-180000))
    monkeypatch.setattr(source, "bybit",
                        lambda *_: fake_response(newest-180000, 100))
    with pytest.raises(DataUnavailable, match="Устаревшие свечи"):
        source.candles(SYMBOL, 100)


def test_hub_client_rejects_unverified_ws_tail_with_typed_error(monkeypatch):
    monkeypatch.setenv("BCC_MARKET_HUB_MODE", "client")
    monkeypatch.setenv("BCC_BYBIT_WS_KLINES", "1")
    class Shared:
        path = "/tmp/synthetic"
        def read_fresh(self, *_):
            return fake_response(int(NOW//60)*60000, 1)
        def close(self): pass
    class Registry:
        def subscribe(self, *_, **__): return "fixture"
    source = PublicData()
    source.shared = Shared()
    source._hub_registry = Registry()
    try:
        with pytest.raises(WebSocketKlineUnavailable, match="WebSocket свеча"):
            source.get(KLINE, {"category": "linear", "symbol": SYMBOL,
                               "interval": "1", "limit": 10})
    finally:
        source.close()


def test_healthy_ws_tail_keeps_fast_path_and_original_candle_time(source, monkeypatch):
    newest = int(NOW//60)*60000
    source.cache[("bars", SYMBOL)] = (NOW-20, bars_ending(newest-60000))
    calls = []
    def ws(route, params):
        calls.append(params["limit"])
        return fake_response(newest, 1)
    monkeypatch.setattr(source, "bybit", ws)
    rows = source.candles(SYMBOL, 100)
    assert calls == [10]
    assert rows[-1]["t"] == newest
