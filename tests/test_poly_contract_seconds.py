"""PAPER schedule invariants, old-config migration, actual engine decisions."""
import math
import time
from copy import deepcopy

import pytest

from bcc.config import (
    defaults, normalize_config, poly_window_allows, resolve_poly_market, validate,
)
from bcc.storage import Store, initial_state
from bcc.worker import process_polymarket
from test_strategies import PolySource


def cfg():
    c = defaults("POLYMARKET")
    c.update(name="Schedule seconds", symbols=["BTC", "ETH"])
    c["polymarket"]["durations"] = [300, 900]
    c["polymarket"].update(entry_signal_mode="before_close",
                           entry_seconds_before_close=45,
                           entry_window_seconds=10,
                           entry_min_seconds_left=5)
    return c


def test_contract_merges_market_after_coin_without_mutation():
    c = cfg()
    c["coin_overrides"] = {"BTC": {"parameters": {"ask_min": .32,
                            "entry_seconds_before_close": 60}}}
    c["polymarket"]["overrides"] = {"BTC:900": {
        "entry_seconds_before_close": 12,
        "entry_window_seconds": 4,
        "entry_min_seconds_left": 2,
        "ask_max": .9,
    }}
    original = deepcopy(c)
    validate(c)
    rules = [resolve_poly_market(c, sym, d) for sym in ("BTC", "ETH")
             for d in (300, 900)]
    assert rules[0]["ask_min"] == .32
    assert rules[0]["entry_seconds_before_close"] == 60
    assert rules[1]["entry_seconds_before_close"] == 12
    assert rules[1]["ask_max"] == .9
    assert rules[2]["entry_seconds_before_close"] == 45
    assert c == original


def test_old_schedule_migrates_effective_windows_at_all_levels():
    c = cfg()
    # Represent a saved v17 bot: the old before-close window extended from
    # N seconds before the end until the expiry-safety cutoff.
    old = c["polymarket"]
    for k in ("entry_signal_mode", "entry_seconds_before_close",
              "entry_window_seconds", "entry_min_seconds_left"):
        old.pop(k)
    old.update(timing="before_close", seconds_before_close=50,
               min_seconds_left=5)
    c["coin_overrides"] = {"BTC": {"parameters": {
        "seconds_before_close": 40,
    }}}
    old["overrides"] = {"BTC:900": {"min_seconds_left": 10}}
    original = deepcopy(c)
    new = validate(c)
    assert c == original
    assert not set(new["polymarket"]) & {"timing", "seconds_before_close", "min_seconds_left"}
    root = resolve_poly_market(new, "ETH", 300)
    coin = resolve_poly_market(new, "BTC", 300)
    market = resolve_poly_market(new, "BTC", 900)
    assert (root["entry_seconds_before_close"], root["entry_window_seconds"],
            root["entry_min_seconds_left"]) == (50, 45, 5)
    assert (coin["entry_seconds_before_close"], coin["entry_window_seconds"],
            coin["entry_min_seconds_left"]) == (40, 35, 5)
    assert (market["entry_seconds_before_close"], market["entry_window_seconds"],
            market["entry_min_seconds_left"]) == (40, 30, 10)
    assert validate(new) == new


@pytest.mark.parametrize("override", [
    {"entry_seconds_before_close": 301},
    {"entry_seconds_before_close": 5},
    {"entry_window_seconds": 46},
    {"entry_window_seconds": 42},
    {"entry_min_seconds_left": 300},
    {"entry_window_seconds": float("nan")},
    {"entry_seconds_before_close": float("inf")},
    {"ask_max": .01, "ask_min": .8},
])
def test_invalid_market_timing_or_ask_is_rejected(override):
    c = cfg()
    c["polymarket"]["overrides"]["BTC:300"] = override
    with pytest.raises((ValueError, TypeError)):
        validate(c)


def test_invalid_market_inherited_ask_is_rejected():
    c = cfg()
    c["coin_overrides"] = {"BTC": {"parameters": {"ask_min": .6}}}
    c["polymarket"]["overrides"]["BTC:300"] = {"ask_max": .5}
    with pytest.raises(ValueError):
        validate(c)


def test_duplicate_durations_rejected():
    c = cfg()
    c["polymarket"]["durations"] = [300, 300]
    with pytest.raises(ValueError):
        validate(c)


def test_window_uses_both_ends_of_clock_uncertainty():
    r = cfg()["polymarket"]
    assert not poly_window_allows(r, 0, 300, 254.99, 255.01)
    assert poly_window_allows(r, 0, 300, 255, 255.2)
    assert poly_window_allows(r, 0, 300, 264.8, 265)
    assert not poly_window_allows(r, 0, 300, 264.9, 265.01)
    assert not poly_window_allows(r, 0, 300, 299, 301)
    for x in (math.nan, math.inf, -math.inf, "255"):
        assert not poly_window_allows(r, 0, 300, x, 255)
    anytime = deepcopy(r)
    anytime["entry_signal_mode"] = "anytime"
    assert poly_window_allows(anytime, 0, 300, 200, 201)
    assert not poly_window_allows(anytime, 0, 300, 299, 300)


class ScheduledSource(PolySource):
    def __init__(self, seconds_left):
        super().__init__()
        self.left = seconds_left

    def discover(self, coin, duration, now):
        result = super().discover(coin, duration, now)
        result["end"] = self.now + self.left
        result["start"] = result["end"] - duration
        return result


@pytest.mark.parametrize("left,should_open", [
    (46, False), (43, True), (35.1, True), (34, False), (5, False),
])
def test_real_engine_respects_seconds_window(left, should_open):
    c = cfg()
    c["symbols"] = ["BTC"]
    c["polymarket"]["durations"] = [300]
    state, events = initial_state(500), []
    source = ScheduledSource(left)
    process_polymarket(c, state, events, source, source.now)
    assert bool(state["positions"]) is should_open, state["diagnostics"]
    if should_open:
        position = next(iter(state["positions"].values()))
        recorded = next(e for e in events if e["action"] == "OPEN")
        assert position["entry_rule"]["entry_seconds_before_close"] == 45
        assert recorded["applied_entry_rule"] == position["entry_rule"]
        assert recorded["entry_ts_utc"] == position["opened"]


def test_open_position_keeps_frozen_schedule_on_edit_and_restart(tmp_path):
    c = cfg()
    c["symbols"] = ["BTC"]
    c["polymarket"]["durations"] = [300]
    src = ScheduledSource(42)
    store = Store(tmp_path, 500)
    with store.transaction() as (state, events):
        process_polymarket(c, state, events, src, src.now)
    old = next(iter(store.read()["positions"].values()))["entry_rule"]
    c["polymarket"].update(entry_seconds_before_close=90,
                           entry_window_seconds=12)
    validate(c)
    with store.transaction() as (state, events):
        process_polymarket(c, state, events, src, src.now + 1)
    store.audit()
    after = next(iter(Store(tmp_path).read()["positions"].values()))
    assert after["entry_rule"] == old


@pytest.mark.parametrize("impulse", [math.nan, math.inf, -math.inf, "bad"])
def test_bad_impulse_never_opens_position(impulse):
    c = cfg()
    c["symbols"] = ["BTC"]
    c["polymarket"]["durations"] = [300]
    src = ScheduledSource(40)
    src.impulse = lambda *args: (impulse, impulse)
    state, events = initial_state(500), []
    process_polymarket(c, state, events, src, src.now)
    assert state["positions"] == {}
    assert state["cash"] == 500
    assert not [e for e in events if e["action"] == "OPEN"]


@pytest.mark.parametrize("payout", [float("nan"), -1, 1.1, "pending"])
def test_bad_official_payout_cannot_settle(payout):
    c = cfg()
    c["symbols"] = ["BTC"]
    c["polymarket"]["durations"] = [300]
    src = ScheduledSource(40)
    state, events = initial_state(500), []
    process_polymarket(c, state, events, src, src.now)
    assert state["positions"]
    cash = state["cash"]
    src.payout = lambda token: payout
    process_polymarket(c, state, events, src, src.now + 41)
    assert state["positions"] and state["cash"] == cash


def test_coin_policy_validation_does_not_change_other_coins_market_rules():
    c = cfg()
    c["coin_overrides"] = {"BTC": {"parameters": {"ask_min": .75, "ask_max": .9}}}
    c["polymarket"]["overrides"] = {"ETH:300": {"ask_max": .6}}
    # ETH inherits global ASK min=.1, not BTC's .75.
    valid = validate(c)
    assert resolve_poly_market(valid, "ETH", 300)["ask_max"] == .6
    assert resolve_poly_market(valid, "BTC", 300)["ask_min"] == .75
