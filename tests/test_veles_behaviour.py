"""Veles mechanics through signal evaluation, execution and a durable account."""

import time
from copy import deepcopy

import pytest
from test_core import attach, bars, config

from bcc.config import coin_section, condition, effective, group, validate
from bcc.funding import apply_funding
from bcc.indicators import evaluate
from bcc.storage import Repository, Store, initial_state
from bcc.strategies import Veles, capital_margin
from bcc.trading import (
    add_order,
    enter,
    exit_position,
    percent_stop_price,
    plan,
    protective_exit,
)


def single_section():
    cfg = config()
    cfg["symbols"] = ["BTCUSDT"]
    cfg["sections"] = cfg["sections"][:1]
    cfg["sections"][0]["entry"] = group(condition("PRICE", 0, ">", 1))
    cfg["sections"][0]["exit"] = group(condition("PRICE", 1e9, ">", 1))
    cfg["settings"].update(
        leverage=1, allocation_pct=10, fee_rate=0.001, slippage_pct=0
    )
    cfg["settings"]["orders"]["count"] = 1
    return cfg


@pytest.mark.parametrize("nested", [False, True])
def test_or_can_enter_from_ready_branch_while_other_indicator_warms_up(nested):
    ready = condition("PRICE", 100, "cross_up", 1)
    warming = dict(condition("RSI", 30, "cross_up", 30), period=500)
    spec = {"operator": "OR", "conditions": [ready, warming]}
    if nested:
        spec = {
            "operator": "OR",
            "conditions": [],
            "groups": [group(ready), group(warming)],
        }
    first = evaluate(spec, bars([99, 101]), 120000, {})
    assert first.available and first.matched
    assert not first.observations[-1]["available"]
    assert not evaluate(spec, bars([99, 101]), 120001, first.clocks).matched
    spec["operator"] = "AND"
    assert not evaluate(spec, bars([99, 101]), 120000, {}).matched


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_percent_stop_adds_offset_to_coverage_and_does_not_close_early(side):
    cfg = single_section()
    cfg["sections"][0]["side"] = side
    cfg["settings"]["orders"].update(count=2, coverage_pct=15, martingale_pct=0)
    cfg["settings"]["stop"].update(mode="both", pct=5, stop_after=True)
    cfg["settings"]["stop"]["signal"] = group(condition("PRICE", 0, "<", 1))
    state, events = initial_state(500), []
    engine = Veles()
    attach(state)
    engine.process(
        cfg, state, events, "BTCUSDT", bars([100, 100]), 99.99, 100.01, 120000
    )
    key = next(iter(state["positions"]))
    close_to_stop = 80.5 if side == "LONG" else 119.5
    attach(state, price=close_to_stop)
    engine.process(
        cfg,
        state,
        events,
        "BTCUSDT",
        bars([100, 100]),
        close_to_stop - 0.01,
        close_to_stop + 0.01,
        120001,
    )
    assert key in state["positions"]
    p = state["positions"][key]
    assert p["next_order"] == 2
    assert percent_stop_price(p) == pytest.approx(
        (100.01 if side == "LONG" else 99.99) * (0.8 if side == "LONG" else 1.2)
    )
    beyond_stop = 79.8 if side == "LONG" else 120.2
    attach(state, price=beyond_stop)
    engine.process(
        cfg,
        state,
        events,
        "BTCUSDT",
        bars([100, 100, 100]),
        beyond_stop - 0.01,
        beyond_stop + 0.01,
        180000,
    )
    assert not state["positions"] and state["done"][key] == "STOP_LOSS"
    assert events[-1]["reason"] == "STOP_LOSS"
    assert "остановлена" in state["diagnostics"]["BTCUSDT"][-1]["reason"]


def test_gap_beyond_stop_closes_immediately_after_last_dca():
    cfg = single_section()
    cfg["settings"]["orders"].update(count=2, coverage_pct=15, martingale_pct=0)
    cfg["settings"]["stop"].update(mode="percent", pct=5, stop_after=True)
    state, events = initial_state(500), []
    engine = Veles()
    attach(state)
    engine.process(
        cfg, state, events, "BTCUSDT", bars([100, 100]), 99.99, 100.01, 120000
    )
    attach(state, price=79)
    engine.process(
        cfg, state, events, "BTCUSDT", bars([100, 100]), 78.99, 79.01, 120001
    )
    assert not state["positions"]
    assert [e["action"] for e in events] == ["OPEN", "DCA", "CLOSE"]
    assert events[-1]["reason"] == "STOP_LOSS"


def test_signal_stop_can_fire_before_grid_completion_when_percent_stop_is_enabled():
    cfg = single_section()
    cfg["settings"]["orders"].update(count=3, coverage_pct=40)
    cfg["settings"]["stop"].update(mode="both", reference="average", stop_after=True)
    cfg["settings"]["stop"]["signal"] = group(condition("PRICE", 99, "<", 1))
    state, events = initial_state(500), []
    engine = Veles()
    attach(state)
    engine.process(
        cfg, state, events, "BTCUSDT", bars([100, 100]), 99.99, 100.01, 120000
    )
    assert next(iter(state["positions"].values()))["next_order"] == 1
    attach(state, price=98)
    engine.process(
        cfg, state, events, "BTCUSDT", bars([100, 100, 98]), 97.99, 98.01, 180000
    )
    assert not state["positions"]
    assert events[-1]["reason"] == "SIGNAL_STOP"


def test_reinvest_uses_only_configured_share_of_closed_trading_profit(
    tmp_path, monkeypatch
):
    clock = [time.time()]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    cfg = single_section()
    cfg["reinvest"] = True
    cfg["settings"]["reinvest_pct"] = 20
    store = Store(tmp_path, 500)
    key = "BTCUSDT:" + cfg["sections"][0]["id"]
    with store.transaction() as (state, events):
        attach(state)
        Veles().process(
            cfg, state, events, "BTCUSDT", bars([100, 100]), 99.99, 100.01, 120000
        )
        state["marks"]["BTCUSDT"] = {"bid": 200, "ask": 200.01, "at": clock[0]}
        assert (
            capital_margin(cfg, state, cfg["settings"]) == 50
        )  # unrealized gain is excluded
        qty = state["positions"][key]["qty"]
        clock[0] += 1
        apply_funding(
            state,
            events,
            "BTCUSDT",
            [{"at": clock[0], "rate": 0.01}],
            {clock[0]: 100},
            clock[0],
        )
        cfg["settings"]["reinvest_pct"] = 50  # existing trade keeps its 20% snapshot
        clock[0] += 1
        attach(state, price=110)
        exit_position(state, events, key, 110, "SIGNAL_EXIT", 0.5)
        assert state["strategy"].get("veles_reinvested", 0) == 0
        attach(state, price=110)
        exit_position(state, events, key, 110, "SIGNAL_EXIT")
        state.pop("_books")
    history = list(reversed(store.history()))
    opened = next(e for e in history if e["action"] == "OPEN")
    closes = [e for e in history if e["action"] in ("CLOSE", "PARTIAL")]
    trading_net = (
        sum((e["price"] - opened["price"]) * e["qty"] - e["fee"] for e in closes)
        - opened["fee"]
    )
    state = Store(tmp_path).read()
    assert state["cash"] == pytest.approx(500 + trading_net - qty * 100 * 0.01)
    assert state["strategy"]["veles_reinvested"] == pytest.approx(trading_net * 0.2)
    assert capital_margin(cfg, state, cfg["settings"]) == pytest.approx(
        (500 + trading_net * 0.2) * 0.1
    )
    assert closes[-1]["trading_net_pnl"] == pytest.approx(trading_net)
    before = state["strategy"]["veles_reinvested"]
    with store.transaction() as (state, events):
        attach(state)
        assert enter(
            state,
            events,
            key,
            "BTCUSDT",
            "LONG",
            cfg["settings"],
            100,
            50,
            plan(cfg["settings"]["orders"], "LONG", 100),
            cfg["sections"][0]["id"],
            reinvest_pct=50,
        )[0]
        attach(state, price=90)
        exit_position(state, events, key, 90, "SIGNAL_EXIT")
        state.pop("_books")
    assert (
        store.read()["strategy"]["veles_reinvested"] == before
    )  # losing cycle adds nothing
    store.audit()


def test_per_coin_section_direction_enablement_and_inheritance_reach_the_engine():
    cfg = single_section()
    cfg["symbols"] = ["BTCUSDT", "ETHUSDT"]
    sec = cfg["sections"][0]
    sec["overrides"] = {"leverage": 4}
    cfg["coin_overrides"] = {
        "BTCUSDT": {
            "settings": {"leverage": 7},
            "sections": {
                sec["id"]: {"side": "SHORT", "settings": {"orders": {"count": 1}}}
            },
        }
    }
    validate(cfg)
    state, events = initial_state(500), []
    for symbol in cfg["symbols"]:
        attach(state, symbol)
        Veles().process(
            cfg, state, events, symbol, bars([100, 100]), 99.99, 100.01, 120000
        )
        # Live runtime validates fresh cross-symbol marks before any next
        # entry. A direct strategy test must provide the same evidence.
        from bcc.market_clock import observed_quote
        state["marks"][symbol] = observed_quote({
            "bid": 99.99, "ask": 100.01, "at": time.time()})
    assert state["positions"]["BTCUSDT:" + sec["id"]]["side"] == "SHORT"
    assert state["positions"]["ETHUSDT:" + sec["id"]]["side"] == "LONG"
    assert effective(cfg, "BTCUSDT", sec)["leverage"] == 4
    cfg["coin_overrides"]["BTCUSDT"]["sections"][sec["id"]]["enabled"] = False
    assert coin_section(cfg, "BTCUSDT", sec)["enabled"] is False
    other, more_events = initial_state(500), []
    attach(other)
    Veles().process(
        cfg, other, more_events, "BTCUSDT", bars([100, 100]), 99.99, 100.01, 120000
    )
    assert not other["positions"]


def test_old_configuration_gets_new_optional_fields_without_mutating_original():
    old = single_section()
    old["settings"].pop("reinvest_pct")
    old["settings"]["stop"].pop("stop_after")
    unchanged = deepcopy(old)
    upgraded = validate(old)
    assert old == unchanged
    assert upgraded["settings"]["reinvest_pct"] == 20
    assert upgraded["settings"]["stop"]["stop_after"] is False


def small_lot_position(targets, quantity):
    cfg = single_section()
    settings = cfg["settings"]
    settings["fee_rate"] = 0
    settings["profit"].update(mode="custom", targets=targets)
    state, events = initial_state(500), []
    attach(state)
    state["_books"]["BTCUSDT"].update(
        qty_step=0.001, min_qty=0.001, asks=[[100, 100]], bids=[[100, 100]]
    )
    assert enter(
        state,
        events,
        "x",
        "BTCUSDT",
        "LONG",
        settings,
        100,
        quantity * 100,
        plan(settings["orders"], "LONG", 100),
        "main",
    )[0]
    return cfg, state, events


def test_unexecutable_small_take_profit_defers_until_next_level_without_locking_exit():
    cfg, state, events = small_lot_position(
        [{"pct": 1, "volume_pct": 50}, {"pct": 2, "volume_pct": 50}], 0.001
    )
    attach(state, price=101.5)
    state["_books"]["BTCUSDT"].update(qty_step=0.001, min_qty=0.001)
    protective_exit(state, events, "x", 101.49, 101.51)
    assert state["positions"]["x"]["qty"] == 0.001
    assert "exit_pending" not in state["positions"]["x"]
    assert state["positions"]["x"]["tp_deferred"]["target"] == 0
    assert len(events) == 1
    attach(state, price=103)
    state["_books"]["BTCUSDT"].update(qty_step=0.001, min_qty=0.001)
    protective_exit(state, events, "x", 102.99, 103.01)
    assert not state["positions"]
    assert [e["action"] for e in events] == ["OPEN", "CLOSE"]
    assert events[-1]["qty"] == 0.001


def test_rounding_from_early_take_profit_carries_into_next_target():
    cfg, state, events = small_lot_position(
        [
            {"pct": 1, "volume_pct": 25},
            {"pct": 2, "volume_pct": 25},
            {"pct": 3, "volume_pct": 50},
        ],
        0.007,
    )
    for price in (101.5, 102.5, 104):
        attach(state, price=price)
        state["_books"]["BTCUSDT"].update(qty_step=0.001, min_qty=0.001)
        protective_exit(state, events, "x", price - 0.01, price + 0.01)
    assert not state["positions"]
    assert [e["qty"] for e in events[1:]] == pytest.approx([0.001, 0.002, 0.004])


def test_restart_recovers_old_too_small_pending_take_profit(tmp_path):
    from bcc.runtime import process_tick
    from bcc.scheduler import MarketTick

    cfg, initial, opened = small_lot_position(
        [{"pct": 1, "volume_pct": 50}, {"pct": 2, "volume_pct": 50}], 0.001
    )
    initial.pop("_books")
    initial["positions"]["x"]["exit_signal"] = cfg["sections"][0]["exit"]
    initial["positions"]["x"]["exit_pending"] = {
        "reason": "MULTI_TP",
        "ratio": 0.5,
        "limit": 101,
        "target_index": 0,
    }
    store = Store(tmp_path, 500)
    with store.transaction() as (state, events):
        state.update(initial)
        events.extend(opened)
    reopened = Store(tmp_path)
    attach(initial, price=103)
    snapshot = initial["_books"]["BTCUSDT"]
    snapshot.update(qty_step=0.001, min_qty=0.001)
    process_tick(cfg, reopened, Veles(), MarketTick("BTCUSDT", [], snapshot, [], {}))
    assert not reopened.read()["positions"]
    assert len(reopened.history()) == 2
    reopened.audit()


def test_grid_reports_minimum_order_failure_instead_of_only_showing_anchor():
    from bcc.strategies import Grid

    cfg = config("GRID")
    cfg["symbols"] = ["BTCUSDT"]
    state, events = initial_state(500), []
    attach(state)
    state["_books"]["BTCUSDT"].update(qty_step=1, min_qty=1)
    Grid().process(cfg, state, events, "BTCUSDT", [], 99.99, 100.01, 120000)
    assert not state["positions"]
    assert any("минимума" in d["reason"] for d in state["diagnostics"]["BTCUSDT"])


def test_finish_policy_preserves_pending_grid_and_supports_cancel_and_restart(tmp_path):
    from bcc.control import arm_run, finish_requested, finished
    from bcc.storage import atomic_json

    cfg = single_section()
    cfg["settings"]["orders"]["first_offset_pct"] = 1
    store = Store(tmp_path, 500)
    with store.transaction() as (state, events):
        arm_run(state, cfg)
        attach(state)
        Veles().process(
            cfg, state, events, "BTCUSDT", bars([100, 100]), 99.99, 100.01, 120000
        )
        assert state["pending"]
        state.pop("_books")
    atomic_json(tmp_path / "finish.request", {"id": "finish", "draining": True})
    finish_requested(tmp_path, store)
    with store.transaction() as (state, events):
        arm_run(state, cfg)  # restarting an unfinished finish keeps the intent
        assert state["strategy"]["draining"]
        attach(state, price=98)
        Veles().process(
            cfg, state, events, "BTCUSDT", bars([100, 100]), 97.99, 98.01, 120001
        )
        assert len(state["positions"]) == 1 and not state["pending"]
        assert not finished(state)
        state.pop("_books")
    atomic_json(tmp_path / "finish.request", {"id": "cancel", "draining": False})
    finish_requested(tmp_path, store)
    assert not store.read()["strategy"]["draining"]
    atomic_json(tmp_path / "finish.request", {"id": "finish_again", "draining": True})
    finish_requested(tmp_path, store)
    with store.transaction() as (state, events):
        attach(state, price=101)
        exit_position(state, events, next(iter(state["positions"])), 101, "SIGNAL_EXIT")
        assert finished(state)
        arm_run(state, cfg)  # a new explicit Start after completed finish rearms
        assert not state["strategy"]["draining"]
        state.pop("_books")
    store.audit()


@pytest.mark.parametrize("disabled", ["section", "coin", "common"])
@pytest.mark.parametrize("action", ["fill", "pull_up"])
def test_disabled_section_can_finish_saved_pending_grid_after_restart(
    tmp_path, disabled, action
):
    from bcc.control import arm_run, finished

    cfg = single_section()
    cfg["settings"]["orders"].update(first_offset_pct=1, pull_up_pct=0.4)
    repo = Repository(tmp_path)
    folder = repo.create(cfg)
    store = Store(folder, cfg["capital"])
    with store.transaction() as (state, events):
        attach(state)
        Veles().process(
            cfg, state, events, "BTCUSDT", bars([100, 100]), 99.99, 100.01, 120000
        )
        assert len(state["pending"]) == 1 and not state["positions"]
        state["strategy"]["draining"] = True
        state.pop("_books")
    cfg, digest = repo.load(folder)
    if disabled == "section":
        cfg["sections"][0]["enabled"] = False
    elif disabled == "coin":
        cfg["coin_overrides"]["BTCUSDT"] = {"settings": {"disabled": True}}
    else:
        cfg["settings"]["disabled"] = True
    repo.save(folder, cfg, digest)
    cfg, _ = repo.load(folder)
    store = Store(folder)
    with store.transaction() as (state, events):
        arm_run(state, cfg)
        price = 98 if action == "fill" else 101
        attach(state, price=price)
        Veles().process(
            cfg,
            state,
            events,
            "BTCUSDT",
            bars([100, price]),
            price - 0.01,
            price + 0.01,
            120001,
        )
        assert not state["pending"]
        assert bool(state["positions"]) == (action == "fill")
        if action == "fill":
            key = next(iter(state["positions"]))
            assert not state["positions"][key]["settings"]["disabled"]
            attach(state, price=101)
            exit_position(state, events, key, 101, "SIGNAL_EXIT")
        assert finished(state)
        state.pop("_books")
    store.audit()


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_trailing_peak_is_rebased_after_dca_without_inventing_price_pullback(side):
    settings = single_section()["settings"]
    settings["orders"].update(
        count=3, mode="signal", martingale_pct=0, signal_reference="entry"
    )
    settings["profit"].update(
        mode="simple", trailing=True, activation_pct=1, distance_pct=0.5
    )
    state, events = initial_state(500), []
    attach(state)
    assert enter(
        state,
        events,
        "x",
        "BTCUSDT",
        side,
        settings,
        100,
        150,
        plan(settings["orders"], side, 100),
        "main",
    )[0]
    first_dca = 80 if side == "LONG" else 120
    attach(state, price=first_dca)
    assert add_order(state, events, "x", first_dca)
    unchanged = 95 if side == "LONG" else 105
    attach(state, price=unchanged)
    assert not protective_exit(state, events, "x", unchanged - 0.01, unchanged + 0.01)
    assert state["positions"]["x"]["trail_peak"] is not None
    assert add_order(state, events, "x", unchanged)
    attach(state, price=unchanged)
    assert not protective_exit(state, events, "x", unchanged - 0.01, unchanged + 0.01)
    assert "x" in state["positions"]
    pullback = 93.5 if side == "LONG" else 106.5
    attach(state, price=pullback)
    assert protective_exit(state, events, "x", pullback - 0.01, pullback + 0.01)
    assert not state["positions"] and events[-1]["reason"] == "TRAILING"
