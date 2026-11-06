"""No unpriced existing PAPER exposure may fund another entry or DCA."""
import math
import time

from bcc.storage import initial_state
from bcc.trading import add_order, available_margin, enter, equity, plan
from test_financial_quote_integrity import opened_linear


def test_missing_existing_position_mark_blocks_new_direct_entry():
    state, events, settings = opened_linear()
    before = len(events)
    assert equity(state)[1] is True
    assert available_margin(state) == -math.inf
    ok, reason = enter(
        state, events, "BTCUSDT:second", "BTCUSDT", "LONG", settings,
        100, 25, plan(settings["orders"], "LONG", 100), "second"
    )
    assert not ok and "капитала" in reason
    assert len(events) == before
    assert "BTCUSDT:second" not in state["positions"]


def test_fresh_proof_restores_admission_without_bypassing_book():
    state, events, settings = opened_linear()
    state["marks"]["BTCUSDT"] = {
        "bid": 99.9, "ask": 100.1, "at": time.time(),
    }
    assert equity(state)[1] is False
    ok, reason = enter(
        state, events, "BTCUSDT:second", "BTCUSDT", "LONG", settings,
        100, 25, plan(settings["orders"], "LONG", 100), "second"
    )
    assert ok, reason


def test_missing_polymarket_quote_never_counted_as_spendable_margin():
    state = initial_state(500)
    state["cash"] = 475
    state["positions"]["BTC:UP"] = {
        "kind": "POLYMARKET", "token": "token-up", "symbol": "BTC",
        "shares": 50.0, "cost": 25.0, "side": "UP", "margin": 0,
    }
    value, unknown = equity(state)
    assert unknown and math.isfinite(value)
    assert available_margin(state) == -math.inf


def test_dca_refuses_unpriced_open_position_without_mutation():
    from test_core import attach, config
    state, events = initial_state(500), []
    settings = config()["settings"]
    settings["orders"].update(count=2, martingale_pct=0)
    attach(state)
    assert enter(state, events, "first", "BTCUSDT", "LONG", settings, 100,
                 25, plan(settings["orders"], "LONG", 100), "first")[0]
    position_before = state["positions"]["first"].copy()
    events_before = len(events)
    assert not add_order(state, events, "first", 80)
    assert len(events) == events_before
    assert state["positions"]["first"] == position_before
    attach(state, price=80)  # Real runtime also refreshes valuation mark.
    assert add_order(state, events, "first", 80)
