"""Provider policy denials are a fail-closed market admission boundary.

These are transport tests only: no trading credentials, live orders or fake
market fills. A legal/geographic 451 must not be treated as a stale candle.
"""

import pytest
import requests

from bcc import health
from bcc.market import PublicData, SourceAccessDenied


class Response:
    def __init__(self, status, payload=None, retry_after=None):
        self.status_code = status
        self.payload = payload or {"ok": True}
        self.headers = {} if retry_after is None else {"Retry-After": retry_after}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("HTTP " + str(self.status_code))

    def json(self):
        return self.payload


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)

    def close(self):
        pass


@pytest.fixture
def isolated_source(monkeypatch):
    monkeypatch.delenv("BCC_PUBLIC_CACHE", raising=False)
    monkeypatch.delenv("BCC_MARKET_HUB_MODE", raising=False)
    source = PublicData()
    yield source
    source.close()


def test_legal_451_denial_blocks_cached_data_and_all_followup_routes(isolated_source):
    session = Session(Response(200, {"valid": True}), Response(451))
    isolated_source._thread.session = session
    base = "https://api.binance.com/api/v3/"
    assert isolated_source.get(base + "time", ttl=300) == {"valid": True}
    with pytest.raises(SourceAccessDenied) as first:
        isolated_source.get(base + "klines", {"symbol": "BTCUSDT"})
    assert first.value.http_status == 451
    assert first.value.host == "api.binance.com"
    with pytest.raises(SourceAccessDenied) as again:
        isolated_source.get(base + "time", ttl=300)
    assert again.value.http_status == 451
    assert len(session.calls) == 2, "a restricted venue must not be retried or served from cache"


def test_403_denial_releases_hub_lease_and_denies_repeated_requests(isolated_source):
    session = Session(Response(403, retry_after="120"))
    isolated_source._thread.session = session

    class Shared:
        def __init__(self):
            self.finished = []
            self.begin_count = 0

        def begin(self, url, params, host, priority=False):
            self.begin_count += 1
            return ("subscription", "lease", None)

        def finish(self, *args, **kwargs):
            self.finished.append((args, kwargs))

        def close(self):
            pass

    shared = Shared()
    isolated_source.shared = shared
    url = "https://api.bybit.com/v5/market/tickers"
    with pytest.raises(SourceAccessDenied) as exc:
        isolated_source.get(url, {"category": "linear", "symbol": "BTCUSDT"})
    assert exc.value.http_status == 403
    assert shared.begin_count == 1
    assert len(shared.finished) == 1
    assert shared.finished[0][1]["cooldown"] == 120
    with pytest.raises(SourceAccessDenied):
        isolated_source.get(url, {"category": "linear", "symbol": "ETHUSDT"})
    assert shared.begin_count == 1
    assert len(session.calls) == 1


def test_denial_expiry_is_monotonic_not_wall_clock(isolated_source, monkeypatch):
    import bcc.market as market

    now = [100.0]
    wall = [10000.0]
    monkeypatch.setattr(market.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(market.time, "time", lambda: wall[0])
    session = Session(Response(451))
    isolated_source._thread.session = session
    url = "https://api.binance.com/api/v3/time"
    with pytest.raises(SourceAccessDenied):
        isolated_source.get(url)
    wall[0] += 1000000.0
    with pytest.raises(SourceAccessDenied):
        isolated_source.get(url)
    assert len(session.calls) == 1


def test_health_preserves_denial_metadata_and_keeps_release_gate_closed(monkeypatch):
    def blocked():
        raise SourceAccessDenied("api.bybit.com", 403)

    monkeypatch.setattr(health, "check_bybit", blocked)
    monkeypatch.setattr(health, "check_polymarket", lambda: {"outcome": "UP"})
    monkeypatch.setattr(health, "check_signal_source", lambda: {"symbol": "BTCUSDT"})
    report = health.check_sources()
    assert report["all_available"] is False
    assert report["source_restrictions"] == [
        {"source": "Bybit USDT Futures", "host": "api.bybit.com", "http_status": 403}
    ]
    assert [x["status"] for x in report["checks"]] == [
        "RESTRICTED", "PASS", "PASS"
    ]
    assert "HTTP 403" in report["checks"][0]["error"]


def test_denial_is_scoped_to_provider_not_other_independent_venues(isolated_source):
    session = Session(Response(451), Response(200, {"ok": "polymarket"}))
    isolated_source._thread.session = session
    with pytest.raises(SourceAccessDenied):
        isolated_source.get("https://api.binance.com/api/v3/time")
    assert isolated_source.get("https://gamma-api.polymarket.com/markets") == {
        "ok": "polymarket"
    }
    assert len(session.calls) == 2


def test_clob_access_denial_is_not_swallowed_as_missing_liquidity(monkeypatch):
    class CLOB:
        def discover(self, *args):
            return {"slug": "market", "start": 0, "end": 300,
                    "tokens": {"UP": "token-up", "DOWN": "token-down"}}

        def execution_terms(self, market):
            return {}

        def book(self, token):
            raise SourceAccessDenied("clob.polymarket.com", 451)

        def close(self):
            pass

    monkeypatch.setattr(health, "PolymarketData", CLOB)
    monkeypatch.setattr(health, "check_bybit", lambda: {"symbol": "BTCUSDT"})
    monkeypatch.setattr(health, "check_signal_source", lambda: {"symbol": "BTCUSDT"})
    report = health.check_sources()
    assert report["all_available"] is False
    assert report["source_restrictions"] == [
        {"source": "Polymarket Gamma + CLOB",
         "host": "clob.polymarket.com", "http_status": 451}
    ]


def test_windows_evidence_names_external_denials_but_cannot_certify_release(tmp_path):
    import json
    from tools.verify_windows import (
        source_access_blockers, validate_required_evidence,
    )

    path = tmp_path / "public_sources.json"
    path.write_text(json.dumps({
        "read_only": True,
        "all_available": False,
        "checks": [
            {"source": "Bybit USDT Futures", "status": "RESTRICTED",
             "restriction": {"host": "api.bybit.com", "http_status": 403}},
            {"source": "Binance spot impulse", "status": "RESTRICTED",
             "restriction": {"host": "api.binance.com", "http_status": 451}},
            {"source": "Polymarket Gamma + CLOB", "status": "PASS",
             "evidence": {"outcome": "UP"}},
        ],
    }), encoding="utf-8")
    blockers = source_access_blockers(path)
    assert {(b["host"], b["http_status"]) for b in blockers} == {
        ("api.bybit.com", 403), ("api.binance.com", 451)
    }
    errors = validate_required_evidence(tmp_path)
    assert any("публичные источники не подтверждены" in x for x in errors)
    assert any("HTTP 403" in x and "Bybit" in x for x in errors)
    assert any("HTTP 451" in x and "Binance" in x for x in errors)


def test_windows_diagnostic_rejects_untrusted_denial_host_and_status(tmp_path):
    import json
    from tools.verify_windows import source_access_blockers

    path = tmp_path / "public_sources.json"
    path.write_text(json.dumps({
        "checks": [
            {"source": "Bybit USDT Futures", "status": "RESTRICTED",
             "restriction": {"host": "private.example", "http_status": 451}},
            {"source": "Binance spot impulse", "status": "PASS",
             "restriction": {"host": "api.binance.com", "http_status": 451}},
            {"source": "Polymarket Gamma + CLOB", "status": "RESTRICTED",
             "restriction": {"host": "clob.polymarket.com", "http_status": True}},
        ]
    }), encoding="utf-8")
    assert source_access_blockers(path) == []
