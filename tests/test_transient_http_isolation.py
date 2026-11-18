"""One failed public HTTP route cannot freeze unrelated PAPER market routes.

These tests use a local SQLite cache and synthetic responses only. No
exchange connectivity, credentials, or live bot data is accessed.
"""
import sqlite3
import time

import pytest
import requests

from bcc.market import DataUnavailable, PublicData

URL = "https://api.bybit.com/v5/market/tickers"


class FakeResponse:
    def __init__(self, status=200, headers=None):
        self.status_code = status
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return {"retCode": 0, "result": {"list": []}}


class RouteSession:
    def __init__(self, failed_symbol="BTCUSDT", *, status=None):
        self.failed_symbol = failed_symbol
        self.status = status
        self.requests = []

    def get(self, url, *, params, **kwargs):
        self.requests.append((url, params["symbol"], kwargs))
        if params["symbol"] == self.failed_symbol:
            if self.status is None:
                raise requests.Timeout("synthetic one-route timeout")
            return FakeResponse(self.status, {"Retry-After": "90"})
        return FakeResponse()


@pytest.fixture
def isolated_shared_market(tmp_path, monkeypatch):
    monkeypatch.setenv("BCC_PUBLIC_CACHE", str(tmp_path / "public-cache.sqlite"))
    monkeypatch.delenv("BCC_MARKET_HUB_MODE", raising=False)
    for name in ("BCC_BYBIT_WS_QUOTES", "BCC_BYBIT_WS_BOOKS", "BCC_BYBIT_WS_KLINES"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path / "public-cache.sqlite"


@pytest.mark.parametrize("failed_status", [None, 500])
def test_one_transport_error_does_not_cooldown_unrelated_bybit_symbol(
    monkeypatch, isolated_shared_market, failed_status
):
    fake = RouteSession(status=failed_status)
    monkeypatch.setattr(PublicData, "session", property(lambda _self: fake))
    failing = PublicData()
    try:
        with pytest.raises(DataUnavailable):
            failing.get(URL, {"category": "linear", "symbol": "BTCUSDT"})
    finally:
        failing.close()
    # The other worker uses a separate PublicData / SQLite connection.
    independent = PublicData()
    try:
        valid = independent.get(URL, {"category": "linear", "symbol": "ETHUSDT"})
        assert valid["retCode"] == 0
        assert [entry[1] for entry in fake.requests] == ["BTCUSDT", "ETHUSDT"]
        with sqlite3.connect(isolated_shared_market) as db:
            assert db.execute(
                "SELECT count(*) FROM cooldown WHERE host='api.bybit.com' AND until>?",
                (time.time(),),
            ).fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM lease").fetchone()[0] == 0
    finally:
        independent.close()


def test_actual_429_still_fences_other_workers(monkeypatch, isolated_shared_market):
    fake = RouteSession(status=429)
    monkeypatch.setattr(PublicData, "session", property(lambda _self: fake))
    first = PublicData()
    try:
        with pytest.raises(DataUnavailable):
            first.get(URL, {"category": "linear", "symbol": "BTCUSDT"})
    finally:
        first.close()
    second = PublicData()
    try:
        with pytest.raises(DataUnavailable):
            second.get(URL, {"category": "linear", "symbol": "ETHUSDT"})
        assert [entry[1] for entry in fake.requests] == ["BTCUSDT"]
        with sqlite3.connect(isolated_shared_market) as db:
            assert db.execute(
                "SELECT count(*) FROM cooldown WHERE host='api.bybit.com' AND until>?",
                (time.time(),),
            ).fetchone()[0] == 1
    finally:
        second.close()
