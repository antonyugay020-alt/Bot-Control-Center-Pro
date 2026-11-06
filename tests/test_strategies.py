import time
from copy import deepcopy

import pytest
from test_core import attach, bars, config

from bcc.config import condition, group
from bcc.storage import Store, initial_state
from bcc.strategies import Grid, PumpDump, Veles
from bcc.trading import reserved
from bcc.worker import process_polymarket


def test_grid_levels_fill_and_close_independently():
    cfg = config("GRID")
    cfg["symbols"] = ["BTCUSDT"]
    state = initial_state(500)
    events = []
    engine = Grid()
    now = int(time.time() * 1000)
    history = bars([100] * 100, now // 60000 * 60000 - 100 * 60000)
    for price in (100, 99, 98, 101):
        attach(state, price=price)
        engine.process(
            cfg, state, events, "BTCUSDT", history, price - 0.01, price + 0.01, now
        )
        now += 2000
    assert not state["positions"]
    assert any(e["action"] == "OPEN" for e in events)
    assert any(e["action"] == "CLOSE" and e["reason"] == "GRID_TP" for e in events)
    assert state["cash"] == pytest.approx(500 + sum(e["net_pnl"] for e in events))


def test_pump_fast_and_confirmed_have_separate_positions():
    cfg = config("PUMP_DUMP")
    cfg["symbols"] = ["BTCUSDT"]
    state = initial_state(500)
    events = []
    engine = PumpDump()
    now = int(time.time() * 1000)
    base = now // 60000 * 60000 - 30 * 60000
    history = bars([100] * 27 + [110, 115, 120], base)
    for b in history[-3:]:
        b["volume"] = 1000
    for price in (120, 119, 118, 114):
        attach(state, price=price)
        engine.process(
            cfg, state, events, "BTCUSDT", history, price - 0.01, price + 0.01, now
        )
        now += 2000
    assert {e["section"] for e in events if e["action"] == "OPEN"} == {
        "fast",
        "confirmed",
    }
    assert any(e["action"] == "CLOSE" for e in events)
    assert state["cash"] == pytest.approx(500 + sum(e["cash_delta"] for e in events))


class PolySource:
    def __init__(self):
        self.now = time.time()
        self.resolved = False

    def discover(self, coin, duration, now):
        return {
            "condition": "one-market",
            "start": self.now - 100,
            "end": self.now + 100,
            "tokens": {"UP": "up-token", "DOWN": "down-token"},
            "slug": "test-market",
            "id": "1",
        }

    def impulse(self, coin, start, now):
        return 100, 0.5

    def book(self, token):
        return [{"price": ".5", "size": "10000"}], self.now

    def execution_terms(self, market, require_fees=True):
        return {
            "fee_schedule": {"rate": 0.0, "exponent": 1} if require_fees else None,
            "min_size": 1,
            "tick_size": 0.01,
        }

    def payout(self, token):
        return 1 if self.resolved else None

    def outcome_quote(self, token):
        return {"bid": 0.5, "ask": 0.51, "at": time.time()}


def test_polymarket_waits_for_official_resolution_and_never_reenters():
    cfg = config("POLYMARKET")
    cfg["symbols"] = ["BTC"]
    cfg["polymarket"]["durations"] = [300]
    state = initial_state(500)
    events = []
    source = PolySource()
    process_polymarket(cfg, state, events, source, source.now)
    assert len(state["positions"]) == 1
    cash = state["cash"]
    process_polymarket(cfg, state, events, source, source.now + 101)
    assert len(state["positions"]) == 1 and state["cash"] == cash
    source.resolved = True
    process_polymarket(cfg, state, events, source, source.now + 102)
    assert not state["positions"]
    assert len([e for e in events if e["action"] == "OPEN"]) == 1
    assert sum(e["net_pnl"] for e in events) == pytest.approx(state["cash"] - 500)


def test_polymarket_unavailable_outcome_preserves_position(tmp_path):
    cfg = config("POLYMARKET")
    cfg["symbols"] = ["BTC"]
    cfg["polymarket"]["durations"] = [300]
    source = PolySource()
    store = Store(tmp_path, 500)
    with store.transaction() as (state, events):
        process_polymarket(cfg, state, events, source, source.now)
    with store.transaction() as (state, events):
        process_polymarket(cfg, state, events, source, source.now + 101)
    store.audit()
    assert len(Store(tmp_path).read()["positions"]) == 1


def test_coin_positions_share_capital_without_double_reservation():
    cfg = config()
    cfg["settings"]["allocation_pct"] = 60
    cfg["sections"] = cfg["sections"][:1]
    sec = cfg["sections"][0]
    sec["entry"] = group(condition("PRICE", 100, "cross_up", 1))
    sec["exit"] = group(condition("PRICE", 200, "cross_up", 1))
    state = initial_state(500)
    events = []
    engine = Veles()
    for symbol in cfg["symbols"]:
        attach(state, symbol, 101)
        engine.process(
            cfg, state, events, symbol, bars([99, 101]), 100.99, 101.01, 120000
        )
        # The real runtime refreshes marks for existing positions before
        # admitting risk on another symbol. Preserve that prerequisite here.
        from bcc.market_clock import observed_quote
        state["marks"][symbol] = observed_quote({
            "bid": 100.99, "ask": 101.01, "at": time.time()})
    assert len(state["positions"]) == 1
    assert reserved(state) == pytest.approx(300, abs=0.01)
    assert "Недостаточно" in state["diagnostics"]["ETHUSDT"][0]["reason"]


def test_nested_threshold_group_keeps_value_without_replaying_cross():
    from bcc.indicators import evaluate

    spec = {
        "operator": "AND",
        "conditions": [],
        "groups": [
            group(condition("PRICE", 100, ">", 2)),
            group(condition("PRICE", 110, "cross_up", 1)),
        ],
    }
    b = bars([101, 101, 105, 105, 111])
    first = evaluate(spec, b, 300000, {})
    assert first.matched
    assert not evaluate(spec, b, 300001, first.clocks).matched


def test_funding_is_idempotent_and_counts_quantity_at_timestamp():
    from bcc.funding import apply_funding
    from bcc.trading import enter, plan

    cfg = config()
    state = initial_state(500)
    events = []
    attach(state)
    enter(
        state,
        events,
        "x",
        "BTCUSDT",
        "LONG",
        cfg["settings"],
        100,
        25,
        plan(cfg["settings"]["orders"], "LONG", 100),
        "x",
    )
    p = state["positions"]["x"]
    at = time.time() + 2
    rates = [{"at": at, "rate": 0.0001}]
    before = state["cash"]
    qty = p["qty"]
    apply_funding(state, events, "BTCUSDT", rates, {at: 100}, at + 1)
    assert state["cash"] == pytest.approx(before - qty * 100 * 0.0001)
    after = state["cash"]
    apply_funding(state, events, "BTCUSDT", rates, {at: 100}, at + 1)
    assert state["cash"] == after


def test_book_rejection_rolls_back_entire_transaction(tmp_path):
    from bcc.trading import enter, plan

    store = Store(tmp_path, 500)
    with pytest.raises(RuntimeError):
        with store.transaction() as (state, events):
            attach(state)
            s = config()["settings"]
            enter(
                state,
                events,
                "x",
                "BTCUSDT",
                "LONG",
                s,
                100,
                25,
                plan(s["orders"], "LONG", 100),
                "x",
            )
            raise RuntimeError("power loss")
    assert store.read()["cash"] == 500 and not store.read()["positions"]


def test_public_polymarket_fee_curve_is_budgeted_per_depth_level():
    from bcc.market import estimate_buy

    cost, shares, fee = estimate_buy(
        [{"price": ".5", "size": "10000"}], 25, 0.8, 0, {"rate": 0.07, "exponent": 1}
    )
    assert cost + fee == pytest.approx(25)
    assert fee == pytest.approx(shares * 0.07 * 0.5 * (1 - 0.5))
    assert shares < 50


def test_changed_intrabar_spec_does_not_reuse_previous_delta():
    from bcc.strategies import signal

    state = initial_state(500)
    spec = group(condition("PRICE", 100, "cross_up", 1))
    spec["conditions"][0]["trigger"] = "intrabar"
    first = signal(state, "x", spec, bars([99, 101]), 90000)
    assert first.matched
    changed = deepcopy(spec)
    changed["conditions"][0]["value"] = 99.5
    assert not signal(state, "x", changed, bars([99, 101]), 90001).matched


def test_partial_take_profit_retry_is_not_executed_twice():
    from bcc.trading import enter, exit_position, plan, protective_exit

    cfg = config()
    settings = cfg["settings"]
    settings["profit"]["mode"] = "custom"
    state = initial_state(500)
    events = []
    attach(state)
    enter(
        state,
        events,
        "x",
        "BTCUSDT",
        "LONG",
        settings,
        100,
        25,
        plan(settings["orders"], "LONG", 100),
        "x",
    )
    state["_books"] = {}
    assert not exit_position(
        state, events, "x", 102, "MULTI_TP", 0.5, target_index=0, limit=101
    )
    pending = state["positions"]["x"]["exit_pending"]
    attach(state, price=101.5)
    assert exit_position(
        state,
        events,
        "x",
        101.5,
        pending["reason"],
        pending["ratio"],
        target_index=pending["target_index"],
        limit=pending["limit"],
    )
    qty = state["positions"]["x"]["qty"]
    attach(state, price=101.5)
    protective_exit(state, events, "x", 101.49, 101.51)
    assert state["positions"]["x"]["qty"] == qty
    assert state["positions"]["x"]["order_margin"] == 0


def test_higher_timeframe_coin_override_requests_sufficient_history():
    from bcc.worker import required_bars

    cfg = config()
    section = cfg["sections"][0]
    c = condition("RSI", 30, "cross_up", 30)
    c["period"] = 100
    cfg["coin_overrides"] = {
        "BTCUSDT": {"sections": {section["id"]: {"entry": group(c)}}}
    }
    assert required_bars(cfg, "BTCUSDT") > 9000
    assert required_bars(cfg, "ETHUSDT") < 1000


def test_polymarket_stop_after_cycle_blocks_the_next_market():
    cfg = config("POLYMARKET")
    cfg["symbols"] = ["BTC"]
    cfg["polymarket"]["durations"] = [300]
    cfg["settings"]["stop_after_cycle"] = True
    state, events = initial_state(500), []
    source = PolySource()
    process_polymarket(cfg, state, events, source, source.now)
    source.resolved = True
    process_polymarket(cfg, state, events, source, source.now + 101)
    assert not state["positions"] and state["done"]["cycle:BTC:300"]
    original_discover = source.discover

    def next_market(*args):
        return dict(
            original_discover(*args), condition="next-market", end=source.now + 500
        )

    source.discover = next_market
    process_polymarket(cfg, state, events, source, source.now + 102)
    assert not state["positions"]
    assert len([e for e in events if e["action"] == "OPEN"]) == 1


def test_polymarket_daily_limit_blocks_new_exposure_after_a_loss():
    cfg = config("POLYMARKET")
    cfg["symbols"] = ["BTC"]
    cfg["polymarket"]["durations"] = [300]
    cfg["settings"]["daily_loss_pct"] = 1
    source = PolySource()
    state, events = initial_state(500), []
    process_polymarket(cfg, state, events, source, source.now)
    assert state["positions"]
    # An official zero payout loses the stake; a fresh market must obey the
    # same daily equity guard as futures strategies.
    source.payout = lambda token: 0
    process_polymarket(cfg, state, events, source, source.now + 101)
    assert not state["positions"]
    assert any(
        "дневной лимит" in d["reason"] for d in state["diagnostics"]["POLYMARKET"]
    )
