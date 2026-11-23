"""Held-position candle history vs 831-coin cold scanner Hub demand.

All tests use synthetic exchange values and disposable SQLite; no network,
Windows bot, paid model, or exchange order is accessed. No loosened guards.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from bcc.market import DataUnavailable, PublicData
from bcc.market_hub import HubRegistry
from bcc.scheduler import MarketScheduler


KLINE = "https://api.bybit.com/v5/market/kline"
RISK = "https://api.bybit.com/v5/market/risk-limit"
BOOK = "https://api.bybit.com/v5/market/orderbook"
NOW = 1_800_000_300.


def req(symbol, limit=100):
    return {"category": "linear", "symbol": symbol, "interval": "1", "limit": limit}


class ImmediateCache:
    def __init__(self, path):
        self.path = Path(path)
        self.replies = []
        self.read_counts = {}

    def read_fresh(self, url, params, host):
        self.replies.append((url, params))
        # REST history: initial cache miss activates a real demand; the
        # following bounded Hub read returns the newly published snapshot.
        # Subsequent get() calls use a new synthetic miss/response pair.
        key = (url, repr(sorted(params.items())) if params else None)
        reads = self.read_counts.get(key, 0) + 1
        self.read_counts[key] = reads
        if (url == KLINE and type(params) is dict
                and params.get("limit") != 10 and reads % 2 == 1):
            return None
        return {"retCode": 0, "_bcc_ws_kline": True, "result": {"list": []}}

    def close(self):
        pass


class RegistryRecorder:
    def __init__(self):
        self.subscriptions = []

    def subscribe(self, url, params, *, lifetime, priority):
        self.subscriptions.append((url, dict(params), lifetime, priority))
        return HubRegistry.identity(url, params)[0]


@pytest.fixture
def hub_client(monkeypatch, tmp_path):
    monkeypatch.setenv("BCC_MARKET_HUB_MODE", "client")
    monkeypatch.setenv("BCC_BYBIT_WS_KLINES", "1")
    monkeypatch.setenv("BCC_BYBIT_WS_QUOTES", "0")
    monkeypatch.setenv("BCC_BYBIT_WS_BOOKS", "0")
    src = PublicData()
    src.shared = ImmediateCache(tmp_path / "synthetic-cache.sqlite")
    rec = RegistryRecorder()
    src._hub_registry = rec
    yield src, rec
    src.close()


@pytest.mark.parametrize("limit", [10, 100, 150, 1000])
def test_held_history_is_short_lived_and_priority_but_scan_is_not(hub_client, limit):
    src, rec = hub_client
    # History is always requested via an authenticated public URL; no
    # borrowed mark-price intent or owner-level position metadata.
    src._thread.position_history = True
    result = src.get(KLINE, req("HELDUSDT", limit))
    assert result["retCode"] == 0
    assert rec.subscriptions[-1] == (KLINE, req("HELDUSDT", limit), 12, True)
    assert getattr(src._thread, "position_mark", False) is False
    src._thread.position_history = False
    src.get(KLINE, req("SCANUSDT", limit))
    assert rec.subscriptions[-1] == (KLINE, req("SCANUSDT", limit), 12, False)


def test_held_history_risk_and_market_budget_remain_independent(hub_client):
    src, rec = hub_client
    src._thread.position_mark = True
    # Mark/collateral intent never grants kline the reserved financial budget.
    assert not src._position_critical_request(KLINE, req("HELDUSDT"))
    assert src._position_critical_request(
        BOOK, {"category": "linear", "symbol": "HELDUSDT", "limit": 50})
    src._thread.position_mark = False
    src._thread.position_history = True
    assert src._position_critical_request(KLINE, req("HELDUSDT"))
    assert not src._position_critical_request(
        RISK, {"category": "linear", "symbol": "HELDUSDT"})
    src._thread.position_history = False
    assert not src._position_critical_request(KLINE, req("HELDUSDT"))


def test_source_priority_context_restored_even_on_failure(hub_client, monkeypatch):
    src, _ = hub_client
    def fail(*_):
        assert src._thread.position_history is True
        raise DataUnavailable("synthetic failed history")
    monkeypatch.setattr(src, "candles", fail)
    with pytest.raises(DataUnavailable, match="synthetic"):
        src.position_candles("HELDUSDT", 150)
    assert src._thread.position_history is False


def test_nested_history_context_retains_outer_intent(hub_client, monkeypatch):
    src, _ = hub_client
    src._thread.position_history = True
    monkeypatch.setattr(src, "candles", lambda *_: [])
    assert src.position_candles("HELDUSDT") == []
    assert src._thread.position_history is True


def test_scheduler_uses_held_history_and_scanner_keeps_normal_transport():
    calls = []
    class Source:
        def position_candles(self, symbol, required):
            calls.append(("held", symbol, required))
            return [dict(t=1)]
        def candles(self, symbol, required):
            calls.append(("scan", symbol, required))
            return [dict(t=2)]
        def orderbook(self, symbol):
            return {"at": NOW}
        def refresh_position_risk(self, symbol):
            return None
    instance = SimpleNamespace(source=Source())
    held = MarketScheduler._fetch(instance, "HELDUSDT", 150, [],
                                  urgent=True, history_only=True)
    assert held.bars[0]["t"] == 1 and held.book is None
    scan = MarketScheduler._fetch(instance, "SCANUSDT", 150, [],
                                  urgent=False, history_only=True)
    assert scan.bars[0]["t"] == 2
    assert calls == [("held", "HELDUSDT", 150), ("scan", "SCANUSDT", 150)]


def test_missing_external_priority_adapter_preserves_candles_contract():
    class Legacy:
        def candles(self, symbol, required):
            return [dict(t=3)]
    result = MarketScheduler._fetch(SimpleNamespace(source=Legacy()),
                                    "LEGACYUSDT", 150, [], urgent=True,
                                    history_only=True)
    assert result.bars[0]["t"] == 3


def test_risk_metadata_precedes_held_history_then_cold_scan(tmp_path):
    registry = HubRegistry(tmp_path / "risk-first.sqlite")
    registry.subscribe(KLINE, req("HELDUSDT"), lifetime=12, priority=True)
    registry.subscribe(RISK, {"category": "linear", "symbol": "HELDUSDT"},
                       lifetime=60, priority=True)
    registry.subscribe(KLINE, req("SCANUSDT"), lifetime=12, priority=False)
    ordered = registry.due(limit=3, reserve_regular=0, include_priority=True)
    assert [v[1] for v in ordered] == [RISK, KLINE, KLINE]
    assert [v[3] for v in ordered] == [True, True, False]
    assert ordered[1][2]["symbol"] == "HELDUSDT"


def test_ws_only_tail_still_not_queued_as_rest(tmp_path):
    registry = HubRegistry(tmp_path / "demand.sqlite")
    registry.subscribe(KLINE, req("HELDUSDT", 10), lifetime=12, priority=True)
    registry.subscribe(KLINE, req("HELDUSDT", 100), lifetime=12, priority=True)
    rows = registry.due(limit=4, reserve_regular=0, include_priority=True,
                        skip_ws_routes=(KLINE,))
    assert len(rows) == 1
    assert rows[0][2]["limit"] == 100
    assert rows[0][3] is True


def test_831_background_history_cannot_outrank_held_candle(tmp_path):
    registry = HubRegistry(tmp_path / "large.sqlite")
    for i in range(831):
        registry.subscribe(KLINE, req(f"COLD{i}USDT"), lifetime=12, priority=False)
    registry.subscribe(KLINE, req("HELDUSDT"), lifetime=12, priority=True)
    ordered = registry.due(limit=6, reserve_regular=2, include_priority=True)
    assert len(ordered) == 6
    assert ordered[0][2]["symbol"] == "HELDUSDT"
    assert ordered[0][3] is True
    assert any(not row[3] for row in ordered)


def test_old_privileged_mark_requests_remain_untouched(hub_client):
    src, rec = hub_client
    src._thread.position_mark = True
    args = {"category": "linear", "symbol": "HELDUSDT"}
    # The ticker result here is fake; only Hub intent and lease are asserted.
    src.get("https://api.bybit.com/v5/market/tickers", args)
    assert rec.subscriptions[-1][-2:] == (60, True)


def test_historical_snapshot_stops_polling_after_success_until_new_demand(tmp_path):
    """One actual GET must not cause twelve seconds of redundant refetches."""
    registry = HubRegistry(tmp_path / "oneshot.sqlite")
    params = req("HELDUSDT", 150)
    key = registry.request_once(KLINE, params, lifetime=12, priority=True)
    assert len(registry.due(limit=5)) == 1
    assert registry.claim_leader("synthetic-owner", lifetime=30)
    token = registry.leader_token("synthetic-owner")
    with registry._connect() as db:
        incarnation = db.execute(
            "SELECT subscription_token FROM hub_subscriptions WHERE k=?", (key,)
        ).fetchone()[0]
    assert registry.finish_owned(key, 3600, "synthetic-owner",
                                 token=token, subscription_token=incarnation)
    assert registry.due(limit=5) == []
    # A passive lease renewal must not rearm a previously delivered history.
    registry.subscribe(KLINE, params, lifetime=12, priority=True)
    assert registry.due(limit=5) == []
    # A real subsequent source request explicitly resumes without waiting
    # for 12s lease expiry or the prior 3600s parked deadline.
    registry.request_once(KLINE, params, lifetime=12, priority=True)
    assert registry.due(limit=5)[0][0] == key


def test_only_historical_kline_can_force_due(tmp_path):
    registry = HubRegistry(tmp_path / "only-history.sqlite")
    with pytest.raises(ValueError, match="Only validated Bybit historical"):
        registry.request_once(RISK,
                              {"category": "linear", "symbol": "HELDUSDT"},
                              lifetime=12, priority=True)
    with pytest.raises(ValueError, match="Only validated Bybit historical"):
        registry.subscribe(BOOK, {"category": "linear", "symbol": "HELDUSDT",
                                  "limit": 50},
                           lifetime=12, priority=True, force_due=True)


def test_hub_rest_history_parks_on_success_and_retries_on_failure(tmp_path):
    from bcc.market_hub import MarketDataHub
    class FakeSource:
        shared = object()
        def close(self): pass
    producer = MarketDataHub(tmp_path / "delay.sqlite", FakeSource(), max_workers=1)
    try:
        assert producer._refresh_delay(KLINE, req("HELDUSDT", 100)) == 3600
        assert producer._refresh_delay(KLINE, req("HELDUSDT", 150), failed=True) == 2
        assert producer._refresh_delay(RISK, {"category": "linear",
                                              "symbol": "HELDUSDT"}) != 3600
    finally:
        producer.close()


def test_client_rearms_rest_only_but_keeps_ws_tail_subscribed(hub_client):
    src, rec = hub_client
    events = []
    def request_once(url, params, *, lifetime, priority):
        events.append((url, dict(params), lifetime, priority))
    rec.request_once = request_once
    src.get(KLINE, req("COLDUSDT", 150))
    assert events == [(KLINE, req("COLDUSDT", 150), 12, False)]
    src.get(KLINE, req("WSUSDT", 10))
    assert len(events) == 1
    assert rec.subscriptions[-1] == (KLINE, req("WSUSDT", 10), 12, False)


def test_expired_rest_lease_rearms_only_on_live_client_demand(hub_client):
    src, rec = hub_client
    events = []
    def request_once(url, params, *, lifetime, priority):
        events.append((url, dict(params), lifetime, priority))
    rec.request_once = request_once
    for _ in range(3):
        src.get(KLINE, req("COLDUSDT", 100))
    # Every NEW worker read is an explicit request, unlike the old
    # background re-poll after the first GET completed.
    assert len(events) == 3
    assert all(v[2:] == (12, False) for v in events)


def test_fresh_shared_rest_snapshot_avoids_duplicate_hub_requests(hub_client):
    src, rec = hub_client
    # Exact cached REST result can be used without even opening a Hub
    # subscription transaction: hundreds of concurrent client reads should
    # not multiply provider traffic after a completed response.
    src.shared.read_fresh = lambda *_: {
        "retCode": 0, "result": {"list": []},
    }
    used = []
    rec.request_once = lambda *args, **kwargs: used.append((args, kwargs))
    assert src.get(KLINE, req("HELDUSDT", 150))["retCode"] == 0
    assert used == []
    assert rec.subscriptions == []
