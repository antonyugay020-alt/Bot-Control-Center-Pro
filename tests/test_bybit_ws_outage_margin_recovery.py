"""Transient WS book loss revokes the quote, not independent risk tiers.

A valid public risk-tier snapshot may survive temporary market unavailability,
but can never authorize collateral without a new authenticated markPrice.
"""
from contextlib import contextmanager
import time

import pytest

from bcc.market_clock import observed_quote
from bcc.margin import MODEL, snapshot, MarginUnavailable
from bcc.runtime import process_tick
from bcc.scheduler import MarketTick
from bcc.storage import initial_state


def test_ws_disconnect_preserves_verified_tiers_but_blocks_margin_then_recovers(monkeypatch):
    from bcc import runtime

    state = initial_state(500)
    state["margin_model"] = MODEL
    state["margin_data"] = {}
    state["margin_data"]["BTCUSDT"] = {
        "symbol": "BTCUSDT",
        "risk_at": time.time(),
        "tiers": [{"verified": "public_Bybit_risk_limit"}],
        "quote": observed_quote({
            "bid": 99., "ask": 99., "at": time.time(),
        }),
    }
    state["marks"]["BTCUSDT"] = observed_quote({
        "bid": 98., "ask": 100., "at": time.time(),
    })

    @contextmanager
    def owned_transaction(_store):
        yield state, []

    monkeypatch.setattr(runtime, "risk_transaction", owned_transaction)

    process_tick({}, object(), None, MarketTick(
        "BTCUSDT", [], None, [], {}, quote_error="Public WebSocket disconnected"
    ))
    assert "BTCUSDT" not in state["marks"]
    preserved = state["margin_data"]["BTCUSDT"]
    assert preserved["tiers"][0]["verified"] == "public_Bybit_risk_limit"
    assert "quote" not in preserved
    with pytest.raises(MarginUnavailable):
        snapshot(state, "BTCUSDT")

    # A newly verified mark restores risk valuation without deleting the
    # independent tier metadata or fabricating mark from bid/ask.
    mark = observed_quote({"bid": 100.5, "ask": 100.5, "at": time.time()})
    book = {
        "at": time.time(),
        "bids": [[100., 10.]],
        "asks": [[101., 10.]],
        "margin_required": True,
        "margin_data": None,
        "margin_marks": {"BTCUSDT": mark},
    }
    process_tick({"symbols": ["BTCUSDT"]}, object(), None, MarketTick(
        "BTCUSDT", [], book, [], {}
    ))
    assert state["margin_data"]["BTCUSDT"]["tiers"] == preserved["tiers"]
    assert snapshot(state, "BTCUSDT")[1] == 100.5


def test_failed_market_book_never_retains_tradeable_margin_quote(monkeypatch):
    from bcc import runtime
    state = initial_state(500)
    state["margin_model"] = MODEL
    state["margin_data"] = {}
    state["margin_data"]["BTCUSDT"] = {
        "symbol": "BTCUSDT",
        "risk_at": time.time(),
        "tiers": [{"verified": True}],
        "quote": observed_quote({
            "bid": 100., "ask": 100., "at": time.time(),
        }),
    }
    @contextmanager
    def transaction(_store):
        yield state, []
    monkeypatch.setattr(runtime, "risk_transaction", transaction)
    process_tick({}, object(), None, MarketTick("BTCUSDT", [], None, [], {}))
    with pytest.raises(MarginUnavailable):
        snapshot(state, "BTCUSDT")
    assert state["margin_data"]["BTCUSDT"]["tiers"]
