"""Stress the public-depth boundary and persisted order lifecycle."""

import threading
import time
from copy import deepcopy

import pytest
from test_core import attach, bars, book, config

from bcc.execution import ExecutionRejected, PaperExecutor
from bcc.market import DataUnavailable
from bcc.runtime import process_tick
from bcc.scheduler import MarketScheduler, MarketTick
from bcc.storage import Store, initial_state
from bcc.strategies import Veles
from bcc.trading import (
    add_order,
    available_margin,
    enter,
    exit_position,
    plan,
    protective_exit,
)


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_ioc_consumes_only_available_depth_while_fok_leaves_book_intact(side):
    settings = config()["settings"]
    settings["slippage_pct"] = 0
    snapshot = book(size=0.1)
    before = deepcopy(snapshot)
    settings["execution_policy"] = "fok"
    with pytest.raises(ExecutionRejected):
        PaperExecutor(snapshot).estimate(side, "EXIT", settings, quantity=1)
    assert snapshot == before
    settings["execution_policy"] = "ioc"
    fill = PaperExecutor(snapshot).estimate(side, "EXIT", settings, quantity=1)
    assert fill.partial and fill.quantity == pytest.approx(0.2)
    assert fill.requested_quantity == 1
    levels = snapshot["bids"] if side == "LONG" else snapshot["asks"]
    assert sum(x[1] for x in levels) == pytest.approx(0)


def open_position(state, events, settings, key="BTCUSDT:main", margin=25):
    attach(state)
    ok, why = enter(
        state,
        events,
        key,
        "BTCUSDT",
        "LONG",
        settings,
        100,
        margin,
        plan(settings["orders"], "LONG", 100),
        "main",
    )
    assert ok, why


def test_partial_exit_survives_store_reopen_and_finishes_without_duplicate_fees(
    tmp_path,
):
    cfg = config()
    cfg["settings"]["orders"]["count"] = 1
    store = Store(tmp_path, 500)
    with store.transaction() as (state, events):
        open_position(state, events, cfg["settings"])
        original = state["positions"]["BTCUSDT:main"]["qty"]
        state["_books"] = {"BTCUSDT": book(105, size=0.1)}
        exit_position(state, events, "BTCUSDT:main", 105, "SIGNAL_EXIT")
        p = state["positions"]["BTCUSDT:main"]
        assert p["qty"] == pytest.approx(original - 0.2)
        assert p["exit_pending"]["ratio"] == 1
        state.pop("_books")
    reopened = Store(tmp_path)
    reopened.audit()

    class NoNewSignals:
        def process(self, *args):
            pass

    tick = MarketTick("BTCUSDT", [], book(106), [], {})
    process_tick(cfg, reopened, NoNewSignals(), tick)
    assert not reopened.read()["positions"]
    executions = list(reversed(reopened.history()))
    assert [e["action"] for e in executions] == ["OPEN", "PARTIAL", "CLOSE"]
    assert executions[1]["fill_status"] == "PARTIAL_IOC"
    assert sum(e["qty"] for e in executions[1:]) == pytest.approx(original)
    assert reopened.read()["cash"] == pytest.approx(
        500 + sum(e["cash_delta"] for e in executions)
    )
    assert reopened.read()["stats"]["BTCUSDT:main"]["cycles"] == 1
    reopened.audit()


def test_incomplete_first_take_profit_cannot_be_overwritten_by_second_target():
    settings = config()["settings"]
    settings["orders"]["count"] = 1
    settings["profit"]["mode"] = "custom"
    state, events = initial_state(500), []
    open_position(state, events, settings)
    state["_books"] = {"BTCUSDT": book(110, size=0.05)}
    assert protective_exit(state, events, "BTCUSDT:main", 109.99, 110.01)
    p = state["positions"]["BTCUSDT:main"]
    assert p["exit_pending"]["target_index"] == 0
    assert not p["targets_done"]
    assert len([e for e in events if e["action"] == "PARTIAL"]) == 1


def test_partially_filled_dca_releases_unfilled_ioc_reservation():
    settings = config()["settings"]
    settings["orders"].update(count=2, martingale_pct=0)
    state, events = initial_state(500), []
    open_position(state, events, settings)
    state["_books"] = {"BTCUSDT": book(89, size=0.025)}
    depth = state["_books"]["BTCUSDT"]
    state["marks"]["BTCUSDT"] = {
        "bid": depth["bids"][0][0], "ask": depth["asks"][0][0],
        "at": depth["at"],
    }
    before = state["positions"]["BTCUSDT:main"]["qty"]
    assert add_order(state, events, "BTCUSDT:main", 89)
    p = state["positions"]["BTCUSDT:main"]
    assert p["qty"] == pytest.approx(before + 0.05)
    assert p["order_margin"] == 0 and p["next_order"] == 2
    assert p["dca_fills"] == 1 and events[-1]["fill_status"] == "PARTIAL_IOC"
    assert p["margin"] < 25


def test_unrealized_loss_prevents_new_margin_reservation():
    settings = config()["settings"]
    settings["orders"]["count"] = 1
    state, events = initial_state(500), []
    open_position(state, events, settings, margin=200)
    state["marks"]["BTCUSDT"] = {"bid": 60, "ask": 60.01, "at": time.time()}
    assert available_margin(state) < 100
    # Refresh execution depth without overwriting the independently observed
    # loss mark; replacing the mark with an entry quote would erase the loss.
    state["_books"] = {"BTCUSDT": book(100)}
    ok, why = enter(
        state,
        events,
        "BTCUSDT:other",
        "BTCUSDT",
        "LONG",
        settings,
        100,
        100,
        plan(settings["orders"], "LONG", 100),
        "other",
    )
    assert not ok and "капитала" in why
    assert len(state["positions"]) == 1


def test_missing_candles_do_not_block_protective_exit_with_fresh_book(tmp_path):
    cfg = config()
    cfg["settings"]["orders"]["count"] = 1
    cfg["settings"]["profit"].update(mode="simple", pct=1)
    store = Store(tmp_path, 500)
    with store.transaction() as (state, events):
        open_position(state, events, cfg["settings"])
        state["positions"]["BTCUSDT:main"]["exit_signal"] = cfg["sections"][0]["exit"]
        state.pop("_books")
    tick = MarketTick("BTCUSDT", [], book(110), [], {}, "История недоступна")
    process_tick(cfg, store, Veles(), tick)
    assert not store.read()["positions"]
    assert store.history()[0]["reason"] == "TAKE_PROFIT"
    assert any(
        d["reason"] == "История недоступна"
        for d in store.read()["diagnostics"]["BTCUSDT"]
    )
    store.audit()


def test_slow_history_of_one_coin_does_not_block_other_coin():
    release = threading.Event()
    entered = threading.Event()

    class IndependentSource:
        stop_event = threading.Event()

        def candles(self, symbol, required):
            if symbol == "SLOW":
                entered.set()
                assert release.wait(3), "test failed to release blocked history"
            return bars([99, 101])

        def orderbook(self, symbol):
            return book()

    scheduler = MarketScheduler(
        IndependentSource(),
        {"symbols": ["SLOW", "FAST"]},
        lambda *args: 50,
        workers=2,
        period=2,
    )
    try:
        results = []
        deadline = time.monotonic() + 1
        while not results and time.monotonic() < deadline:
            results.extend(scheduler.collect(initial_state(500), timeout=0.02))
        assert entered.is_set()
        assert [(s, err) for s, _, err in results] == [("FAST", None)]
        assert "SLOW" in scheduler.jobs
    finally:
        release.set()
        scheduler.close()


def test_scheduler_preserves_quote_when_history_is_unavailable():
    class Source:
        def candles(self, *args):
            raise DataUnavailable("gapped history")

        def orderbook(self, *args):
            return book()

    scheduler = MarketScheduler(
        Source(), {"symbols": ["BTCUSDT"]}, lambda *a: 10, workers=1
    )
    try:
        completed = scheduler.collect(initial_state(500), timeout=1)
        symbol, tick, error = completed[0]
        assert error is None and tick.history_error == "gapped history"
        assert not tick.bars and tick.book["bids"]
    finally:
        scheduler.close()


def test_late_published_funding_adjusts_closed_cycle_once(tmp_path, monkeypatch):
    from bcc.funding import apply_funding

    clock = [time.time()]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    settings = config()["settings"]
    settings["orders"]["count"] = 1
    store = Store(tmp_path, 500)
    with store.transaction() as (state, events):
        open_position(state, events, settings)
        qty = state["positions"]["BTCUSDT:main"]["qty"]
        funding_at = clock[0] + 1
        clock[0] += 2
        attach(state, price=105)
        exit_position(state, events, "BTCUSDT:main", 105, "SIGNAL_EXIT")
        state.pop("_books")
    before = store.read()
    assert before["funding_archive"] and not before["positions"]
    rates = [{"at": funding_at, "rate": 0.0001}, {"at": clock[0] + 1, "rate": 0.0001}]
    clock[0] += 3
    with store.transaction() as (state, events):
        apply_funding(
            state, events, "BTCUSDT", rates, {r["at"]: 100 for r in rates}, clock[0]
        )
    after = store.read()
    charge = qty * 100 * 0.0001
    assert after["cash"] == pytest.approx(before["cash"] - charge)
    assert after["stats"]["BTCUSDT:main"]["net_pnl"] == pytest.approx(
        before["stats"]["BTCUSDT:main"]["net_pnl"] - charge
    )
    assert not after["funding_archive"]
    with store.transaction() as (state, events):
        apply_funding(
            state, events, "BTCUSDT", rates, {r["at"]: 100 for r in rates}, clock[0]
        )
    assert store.read()["cash"] == after["cash"]
    store.audit()


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_last_take_profit_closes_rounding_remainder(side):
    settings = config()["settings"]
    settings["orders"]["count"] = 1
    settings["slippage_pct"] = 0
    settings["profit"]["mode"] = "custom"
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
        25,
        plan(settings["orders"], side, 100),
        "main",
    )[0]
    original = state["positions"]["x"]["qty"]
    price = 103 if side == "LONG" else 97
    attach(state, price=price)
    protective_exit(state, events, "x", price - 0.01, price + 0.01)
    assert not state["positions"]
    assert sum(
        e["qty"] for e in events if e["action"] in ("PARTIAL", "CLOSE")
    ) == pytest.approx(original)
    assert state["stats"]["x"]["cycles"] == 1


def test_removed_coin_still_gets_funding_without_new_market_orders():
    calls = []

    class Source:
        def candles(self, symbol, required):
            calls.append(("candles", symbol))
            return bars([99, 101])

        def orderbook(self, symbol):
            calls.append(("book", symbol))
            return book()

        def funding(self, symbol):
            calls.append(("funding", symbol))
            return []

    state = initial_state(500)
    state["funding_archive"] = {
        "old": {"symbol": "REMOVED", "opened": time.time() - 100}
    }
    scheduler = MarketScheduler(
        Source(), {"symbols": ["ACTIVE"]}, lambda *a: 10, workers=2
    )
    try:
        completed = []
        until = time.monotonic() + 1
        while len(completed) < 2 and time.monotonic() < until:
            completed.extend(scheduler.collect(state, timeout=0.02))
        tick = next(tick for symbol, tick, error in completed if symbol == "REMOVED")
        assert tick.book is None and not tick.bars
        assert ("funding", "REMOVED") in calls
        assert ("candles", "REMOVED") not in calls and ("book", "REMOVED") not in calls
    finally:
        scheduler.close()


def test_open_position_refresh_does_not_wait_for_large_coin_scan():
    fetched = []

    class Source:
        def candles(self, symbol, required):
            fetched.append(symbol)
            return bars([99, 101])

        def orderbook(self, symbol):
            return book()

        def funding(self, symbol):
            return []

    universe = [f"COIN{i}" for i in range(813)]
    state = initial_state(500)
    state["positions"]["tail:main"] = {"symbol": universe[-1], "opened": time.time()}
    scheduler = MarketScheduler(
        Source(), {"symbols": universe}, lambda *a: 50, workers=2, period=0
    )
    try:
        # Futures are submitted concurrently: completing a priority refresh
        # does not prove the scan worker has started. Wait for both observable
        # outcomes rather than depending on the scheduler of the host OS.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            scheduler.collect(state, timeout=0.05)
            if fetched.count(universe[-1]) >= 2 and universe[0] in fetched:
                break
        assert fetched.count(universe[-1]) >= 2
        assert universe[0] in fetched  # Entry scanning still progresses.
        assert len(set(fetched)) < len(universe)
    finally:
        scheduler.close()


def test_five_pending_manual_exits_complete_with_813_charts_and_fresh_depth(tmp_path):
    import json
    import os
    from pathlib import Path

    from bcc.data_cache import PublicCache
    from bcc.market import PublicData
    from bcc.runtime import run_loop

    cfg = config()
    symbols = [f"COIN{i}USDT" for i in range(813)]
    cfg["symbols"] = symbols
    cfg["settings"]["orders"]["count"] = 1
    store = Store(tmp_path, 500)
    history = bars(list(range(80, 200)))
    with store.transaction() as (state, events):
        for symbol in symbols[:5]:
            depth = book()
            state["_books"] = {symbol: depth}
            state["marks"][symbol] = {
                "bid": depth["bids"][0][0], "ask": depth["asks"][0][0],
                "at": depth["at"],
            }
            ok, why = enter(
                state,
                events,
                f"{symbol}:main",
                symbol,
                "LONG",
                cfg["settings"],
                100,
                5,
                plan(cfg["settings"]["orders"], "LONG", 100),
                "main",
            )
            assert ok, why
            state["positions"][f"{symbol}:main"]["exit_pending"] = {
                "reason": "MANUAL_PAPER_CLOSE",
                "ratio": 1.0,
                "limit": None,
                "target_index": None,
            }
        state.pop("_books")
        state["charts"] = {symbol: history for symbol in symbols}
        state["strategy"]["draining"] = True
    quote_threads = []

    class Session:
        def get(self, url, **kwargs):
            data = history if "kline" in url else book(101)
            return type(
                "Response",
                (),
                {
                    "status_code": 200,
                    "headers": {},
                    "raise_for_status": lambda s: None,
                    "json": lambda s: data,
                },
            )()

    class Source(PublicData):
        @property
        def session(self):
            return Session()

        def candles(self, symbol, required):
            return self.get("https://api.bybit.com/v5/market/kline", {"symbol": symbol})

        def orderbook(self, symbol):
            quote_threads.append(threading.current_thread().name)
            snapshot = deepcopy(
                self.get(
                    "https://api.bybit.com/v5/market/orderbook", {"symbol": symbol}
                )
            )
            if threading.current_thread().name != "MainThread":
                snapshot["at"] -= 5  # Simulate a quote that aged in the decision queue.
            return snapshot

        def funding(self, symbol):
            return []

    source = Source()
    source.shared = PublicCache(tmp_path / "public_cache.sqlite")
    started = time.monotonic()
    try:
        run_loop(
            tmp_path,
            cfg,
            store,
            source,
            Veles(),
            {},
            lambda: time.monotonic() - started > 8,
            lambda *a: 150,
            None,
        )
        assert not store.read(False)["positions"]
        assert len(store.chart_symbols()) == 813
        exits = [e for e in store.history() if e.get("reason") == "MANUAL_PAPER_CLOSE"]
        assert len(exits) == 5
        assert len(store.history()) == 10
        assert threading.current_thread().name in quote_threads
        # With exposure-only draining, the bounded position-quotes pool
        # replaces scanner workers without weakening manual-close evidence.
        assert any(name.startswith(("public-market", "position-quotes"))
                   for name in quote_threads)
        store.audit()
        if os.environ.get("BCC_TEST_ARTIFACTS"):
            target = Path(os.environ["BCC_TEST_ARTIFACTS"]) / "manual_close_813.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(
                    {
                        "data": "Test transport, not live market execution",
                        "charts_preserved": 813,
                        "manual_exits_completed": 5,
                        "executions": 10,
                        "elapsed_seconds": time.monotonic() - started,
                    }
                ),
                encoding="utf-8",
            )
    finally:
        source.close()
