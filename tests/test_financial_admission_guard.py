"""Low-level PAPER admission must fail closed on malformed numeric state.

Tests use synthetic accounts and local books only; never user ledgers or APIs.
"""
import math
from copy import deepcopy

import pytest

from bcc.storage import initial_state
from bcc.trading import add_order, available_margin, enter, plan
from test_core import attach, config


@pytest.mark.parametrize("corruption", [
    lambda s: s.update(cash=math.nan),
    lambda s: s.update(cash=math.inf),
    lambda s: s["pending"].update(stale={"margin": math.nan}),
    lambda s: s["pending"].update(stale={"margin": math.inf}),
    lambda s: s["pending"].update(stale={"margin": -5.0}),
    lambda s: s["pending"].update(stale={"margin": "unreadable"}),
])
def test_invalid_capital_cannot_enter_or_mutate_synthetic_account(corruption):
    state, events = initial_state(500), []
    attach(state)
    corruption(state)
    original = deepcopy(state)
    settings = config()["settings"]
    settings["orders"]["count"] = 1
    assert available_margin(state) == -math.inf
    ok, reason = enter(
        state, events, "BTCUSDT:one", "BTCUSDT", "LONG", settings, 100, 25,
        plan(settings["orders"], "LONG", 100), "one",
    )
    assert not ok and "капитала" in reason
    assert repr(state) == repr(original)
    assert not events


@pytest.mark.parametrize("margin,price", [
    (math.nan, 100), (math.inf, 100), (-1, 100), (0, 100),
    (25, math.nan), (25, math.inf), (25, 0),
])
def test_malformed_entry_request_never_mutates_account(margin, price):
    state, events = initial_state(500), []
    attach(state)
    settings = config()["settings"]
    settings["orders"]["count"] = 1
    before = deepcopy(state)
    ok, _ = enter(state, events, "BTCUSDT:x", "BTCUSDT", "LONG",
                  settings, price, margin,
                  plan(settings["orders"], "LONG", 100), "x")
    assert not ok and not events and state == before


def test_malformed_grid_budget_cannot_append_dca_execution():
    state, events = initial_state(500), []
    attach(state)
    settings = config()["settings"]
    settings["orders"]["count"] = 2
    assert enter(state, events, "BTCUSDT:x", "BTCUSDT", "LONG",
                 settings, 100, 25, plan(settings["orders"], "LONG", 100), "x")[0]
    state["positions"]["BTCUSDT:x"]["budget"] = math.nan
    before = deepcopy(state["positions"])
    before_cash = state["cash"]
    assert not add_order(state, events, "BTCUSDT:x", 95)
    assert state["positions"].keys() == before.keys()
    assert state["positions"]["BTCUSDT:x"]["qty"] == before["BTCUSDT:x"]["qty"]
    assert state["cash"] == before_cash and len(events) == 1
