"""Corrupted/stale persisted marks cannot become spendable PAPER collateral."""
import math
import time
from copy import deepcopy

import pytest

from bcc.storage import initial_state
from bcc.trading import (available_margin, enter, equity, liquidations, plan,
                         valid_position_mark)
from test_core import attach, config


def opened_linear():
    state, events = initial_state(500), []
    settings = config()["settings"]
    settings["orders"]["count"] = 1
    attach(state)
    ok, why = enter(state, events, "BTCUSDT:one", "BTCUSDT", "LONG",
                    settings, 100, 25, plan(settings["orders"], "LONG", 100),
                    "one")
    assert ok, why
    return state, events, settings


@pytest.mark.parametrize("mark", [
    {"bid": math.nan, "ask": 100.1},
    {"bid": 99, "ask": math.inf},
    {"bid": 102, "ask": 101},
    {"bid": -1, "ask": 100},
    {"bid": "99", "ask": 100},
    {"bid": None, "ask": 100},
    {"bid": 99},
    {"bid": True, "ask": 100},
])
def test_invalid_linear_mark_cannot_admit_additional_paper_margin(mark):
    state, events, settings = opened_linear()
    original_position = deepcopy(state["positions"]["BTCUSDT:one"])
    state["marks"]["BTCUSDT"] = {"at": time.time(), **mark}
    assert not valid_position_mark(original_position, state["marks"]["BTCUSDT"])
    provisional, unknown = equity(state)
    assert unknown and math.isfinite(provisional)
    assert available_margin(state) == -math.inf
    # No exit or artificial liquidation may be created from a broken mark.
    assert liquidations(state, events) is False
    assert state["positions"]["BTCUSDT:one"] == original_position
    assert len(events) == 1
    ok, message = enter(
        state, events, "BTCUSDT:two", "BTCUSDT", "LONG", settings, 100, 25,
        plan(settings["orders"], "LONG", 100), "two")
    assert not ok and "капитала" in message
    assert list(state["positions"]) == ["BTCUSDT:one"] and len(events) == 1


def test_stale_valid_mark_must_not_fund_new_margin():
    state, events, settings = opened_linear()
    state["marks"]["BTCUSDT"] = {
        "bid": 97.0, "ask": 97.1, "at": time.time() - 600,
    }
    value, unknown = equity(state)
    assert unknown and math.isfinite(value)
    assert available_margin(state) == -math.inf
    assert not enter(state, events, "BTCUSDT:two", "BTCUSDT", "LONG",
                     settings, 100, 50, plan(settings["orders"], "LONG", 100),
                     "two")[0]


def test_fresh_valid_quote_still_prices_loss_and_permits_normal_accounting():
    state, _, _ = opened_linear()
    p = state["positions"]["BTCUSDT:one"]
    state["marks"]["BTCUSDT"] = {
        "bid": 90.0, "ask": 90.1, "at": time.time(),
    }
    assert valid_position_mark(p, state["marks"]["BTCUSDT"])
    assert equity(state) == pytest.approx(
        (state["cash"] + (90.0 - p["average"]) * p["qty"], False))
    assert available_margin(state) == pytest.approx(
        equity(state)[0] - p["margin"] - p["order_margin"])


@pytest.mark.parametrize("bad", [math.nan, math.inf, -0.1, 1.2, "0.5"])
def test_broken_polymarket_mark_is_provisional_not_numerical_equity(bad):
    state = initial_state(500)
    state["cash"] = 475
    state["positions"]["BTC:300:UP"] = {
        "kind": "POLYMARKET", "token": "up-token", "symbol": "BTC",
        "shares": 45.0, "cost": 24.0, "side": "UP", "margin": 0,
    }
    state["marks"]["up-token"] = {
        "bid": bad, "ask": 0.9, "at": time.time(),
    }
    value, unknown = equity(state)
    assert unknown and math.isfinite(value)
    assert value == pytest.approx(499.0)
    assert available_margin(state) == -math.inf

@pytest.mark.parametrize("bid,ask", [
    (math.nan, 100.0), (100.0, math.inf), (101.0, 100.0),
    (-1.0, 100.0), ("99", 100.0),
])
def test_runtime_rejects_broken_book_before_account_transaction(tmp_path, bid, ask):
    from bcc.market import DataUnavailable
    from bcc.runtime import process_tick
    from bcc.scheduler import MarketTick
    from bcc.storage import Store
    from test_core import book

    store = Store(tmp_path, 500)
    before = store.read()
    snapshot = book(100)
    snapshot["bids"][0][0] = bid
    snapshot["asks"][0][0] = ask

    class NeverCalled:
        def process(self, *args):
            raise AssertionError("bad quote reached strategy")

    with pytest.raises(DataUnavailable, match="Повреждена котировка"):
        process_tick(config(), store, NeverCalled(),
                     MarketTick("BTCUSDT", [], snapshot, [], {}))
    assert store.read() == before and store.history() == []
    store.audit()
