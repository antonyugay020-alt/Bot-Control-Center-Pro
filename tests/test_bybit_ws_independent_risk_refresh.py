"""Independent public risk tiers plus live authenticated Bybit WS markPrice.

Tier REST reads are admitted on the history lane with a 120-second single-flight
window. The fast price lane commits newest mark-price without reusing old risk
tier timestamps or treating bid/ask as collateral.
"""
import time

import pytest

from bcc.market import PublicData, DataUnavailable
from bcc.market_clock import observed_quote, quote_is_recent
from bcc.margin import MODEL, attach_snapshot, snapshot, MarginUnavailable
from bcc.scheduler import MarketScheduler


def risk(symbol="BTCUSDT", mark=101.):
    now = time.time()
    return dict(
        symbol=symbol, risk_at=now,
        tiers=[{"dummy": "tier-with-real-source-evidence"}],
        quote=observed_quote({"bid": mark, "ask": mark, "at": now}),
        account_marks={symbol: observed_quote(
            {"bid": mark, "ask": mark, "at": now})},
    )


def test_risk_tier_refresh_is_throttled_and_off_fast_position_lane(monkeypatch):
    source = PublicData()
    calls = []
    monkeypatch.setattr(source, "margin_snapshot", lambda symbol: (
        calls.append(symbol), risk(symbol)
    )[1])
    try:
        first = source.refresh_position_risk("BTCUSDT")
        assert first["symbol"] == "BTCUSDT"
        assert "account_marks" not in first
        assert source.refresh_position_risk("BTCUSDT") is None
        assert calls == ["BTCUSDT"]
        # The strategy history lane can request a new tier verification after
        # the previous public timestamp's 120s renewal window.
        source._risk_refresh_due["BTCUSDT"] = 0
        assert source.refresh_position_risk("BTCUSDT") is not None
        assert calls == ["BTCUSDT", "BTCUSDT"]
    finally:
        source.close()


def test_risk_refresh_failure_cannot_extend_old_tier_timestamp(monkeypatch):
    source = PublicData()
    def unavailable(symbol):
        raise DataUnavailable("Bybit public risk tiers unavailable")
    monkeypatch.setattr(source, "margin_snapshot", unavailable)
    try:
        with pytest.raises(DataUnavailable):
            source.refresh_position_risk("BTCUSDT")
        assert source.refresh_position_risk("BTCUSDT") is None
    finally:
        source.close()


def test_single_owner_merges_fresh_ws_mark_with_independent_existing_tiers():
    now = time.time()
    old_quote = observed_quote({"bid": 88., "ask": 88., "at": now - 9})
    state = {
        "margin_model": MODEL,
        "margin_data": {"BTCUSDT": {
            "symbol": "BTCUSDT",
            "tiers": [{"dummy": "preserved"}],
            "risk_at": now,
            "quote": old_quote,
        }},
        "positions": {"BTCUSDT:one": {"symbol": "BTCUSDT"}},
        "pending": {},
    }
    with pytest.raises(MarginUnavailable, match="mark-price"):
        snapshot(state, "BTCUSDT")
    mark = observed_quote({"bid": 101., "ask": 101., "at": now})
    attach_snapshot(state, "BTCUSDT", {
        "margin_required": True,
        "margin_data": None, "margin_marks": {"BTCUSDT": mark},
    })
    assert state["margin_data"]["BTCUSDT"]["tiers"] == [
        {"dummy": "preserved"}
    ]
    assert quote_is_recent(state["margin_data"]["BTCUSDT"]["quote"], maximum=3)
    assert snapshot(state, "BTCUSDT")[1] == 101.


def test_history_lane_carries_risk_snapshot_without_fake_executable_book():
    class Adapter:
        def candles(self, symbol, required):
            return []
        def refresh_position_risk(self, symbol):
            return risk(symbol)
        def funding(self, symbol):
            return []
    source = Adapter()
    scheduler = MarketScheduler(
        source, {"symbols": ["BTCUSDT"]}, lambda *args: 1, workers=1
    )
    try:
        tick = scheduler._fetch(
            "BTCUSDT", 1, [], manage=True, urgent=True,
            history_only=True
        )
        assert tick.book is None
        assert tick.margin_data["symbol"] == "BTCUSDT"
        assert tick.margin_data["tiers"]
    finally:
        scheduler.close()
