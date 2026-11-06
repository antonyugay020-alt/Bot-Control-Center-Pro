"""Regression: unpriced overnight exposure cannot reset daily risk."""
from copy import deepcopy
import time
import pytest
from bcc.storage import initial_state
from bcc.strategies import daily_ok
from bcc.market_clock import observed_quote

@pytest.mark.parametrize("strategy", ["VELES", "GRID", "PUMP_DUMP", "POLYMARKET"])
def test_unknown_equity_does_not_rebase_daily_limit(strategy):
    state = initial_state(500)
    state["positions"]["BTC:1"] = {
        "kind": "POLYMARKET", "symbol": "BTC", "token": "token-1",
        "cost": 200., "shares": 400., "side": "UP", "section": "BTC:300",
    }
    state["cash"] = 300.
    state["daily"] = {"day": "0", "start": 500.}
    start = deepcopy(state["daily"])
    settings = {"daily_loss_pct": 20}
    assert daily_ok({"strategy":strategy}, state, "BTC", settings, 86400000) is False
    assert state["daily"] == start
    # A valid current quote then permits an accurately valued daily baseline.
    state["marks"]["token-1"] = observed_quote({
        "bid": 0.5, "ask": 0.51, "at": time.time(),
    })
    assert daily_ok({"strategy":strategy}, state, "BTC", settings, 86400000) is True
    assert state["daily"] == {"day": "1", "start": 500.0}

@pytest.mark.parametrize("strategy", ["VELES", "GRID", "PUMP_DUMP", "POLYMARKET"])
def test_existing_daily_limit_not_overwritten_on_missing_valuation(strategy):
    state = initial_state(500)
    state["positions"]["BTC:1"] = {
        "kind": "POLYMARKET", "symbol": "BTC", "token": "token-1",
        "cost": 200., "shares": 400., "side": "UP", "section": "BTC:300",
    }
    state["cash"] = 300.
    state["daily"] = {"day": "1", "start": 400.}
    assert daily_ok({"strategy": strategy}, state, "BTC", {"daily_loss_pct": 20}, 86400000) is False
    assert state["daily"] == {"day": "1", "start": 400.}
