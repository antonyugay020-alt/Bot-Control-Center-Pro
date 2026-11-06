import math
import random
import time
from copy import deepcopy

import pytest

from bcc.config import condition, defaults, effective, group, range_section, validate
from bcc.execution import ExecutionRejected, PaperExecutor
from bcc.indicators import CATALOG, aggregate, evaluate, series
from bcc.storage import BotBusy, BotLock, Repository, Store, initial_state
from bcc.strategies import Veles
from bcc.trading import add_order, enter, exit_position, plan, reserved


def config(kind="VELES"):
    c = defaults(kind)
    c["name"] = "Test"
    return c


def book(price=100, size=10000):
    return dict(
        at=time.time(),
        bids=[[price - 0.01, size], [price - 0.02, size]],
        asks=[[price + 0.01, size], [price + 0.02, size]],
        qty_step=0.0001,
        min_qty=0.0001,
        min_notional=0.01,
        tick_size=0.01,
    )


def attach(state, symbol="BTCUSDT", price=100):
    """Simulate one live tick: refresh both depth and the valuation mark.

    The runtime publishes a fresh mark before attempting DCA or a new
    entry. A test that deliberately needs unknown collateral must omit this
    refresh, as a real missing-market-data scenario would.
    """
    depth = book(price)
    state["_books"] = {symbol: depth}
    if any(p.get("symbol") == symbol for p in state["positions"].values()):
        state["marks"][symbol] = {
            "bid": depth["bids"][0][0],
            "ask": depth["asks"][0][0],
            "at": depth["at"],
        }


def bars(values, base=0):
    return [
        dict(
            t=base + i * 60000,
            open=v,
            high=v + 1,
            low=v - 1,
            close=v,
            volume=10 + i % 3,
        )
        for i, v in enumerate(values)
    ]


@pytest.mark.parametrize(
    "kind",
    [
        "VELES",
        "POLYMARKET",
        "PUMP_DUMP",
        "GRID",
        "EMA",
        "MACD",
        "BOLLINGER",
        "EMA_RSI_VOLUME",
    ],
)
def test_configuration_validates(kind):
    assert validate(config(kind))["strategy"] == kind


@pytest.mark.parametrize(
    "field,value", [("capital", True), ("capital", float("nan")), ("capital", 0)]
)
def test_bad_money_rejected(field, value):
    c = config()
    c[field] = value
    with pytest.raises(ValueError):
        validate(c)


def test_coin_and_section_override_hierarchy():
    c = config()
    s = c["sections"][0]
    s["overrides"] = {"leverage": 4}
    c["coin_overrides"] = {
        "BTCUSDT": {
            "settings": {"orders": {"count": 5}},
            "sections": {s["id"]: {"settings": {"leverage": 7}}},
        }
    }
    assert effective(c, "BTCUSDT", s)["leverage"] == 7
    assert effective(c, "BTCUSDT", s)["orders"]["count"] == 5
    assert effective(c, "ETHUSDT", s)["orders"]["count"] == 10
    validate(c)


def test_two_minute_aggregation_never_leaks_unfinished_close():
    b = bars([100, 101, 102, 110])
    assert [x["close"] for x in aggregate(b, 2, 3 * 60000)] == [101]
    assert [x["close"] for x in aggregate(b, 2, 3 * 60000, "intrabar")] == [101, 110]
    assert aggregate(b, 2, 4 * 60000)[-1]["close"] == 110
    assert aggregate(b[1:], 2, 4 * 60000)[0]["t"] == 120000


def test_no_cross_on_insufficient_history_or_gaps():
    c = condition("PRICE", 100, "cross_up", 1)
    g = group(c)
    assert not evaluate(g, bars([101]), 60000, {}).matched
    b = bars([99, 101])
    b[-1]["t"] = 120000
    assert not evaluate(g, b, 180000, {}).matched


def test_bar_close_clock_and_intrabar_edges_are_independent():
    b = bars([99, 101])
    g = group(condition("PRICE", 100, "cross_up", 1))
    first = evaluate(g, b, 120000, {})
    assert first.matched
    assert not evaluate(g, b, 120001, first.clocks).matched
    g["conditions"][0]["trigger"] = "intrabar"
    first = evaluate(g, b, 90000, {})
    assert first.matched
    assert not evaluate(g, b, 90001, first.clocks).matched
    b[-1]["close"] = 99
    b[-1]["open"] = 99
    b[-1]["low"] = 98
    b[-1]["high"] = 100
    down = evaluate(g, b, 90002, first.clocks)
    assert not down.matched
    b[-1].update(open=101, close=101, low=100, high=102)
    assert evaluate(g, b, 90003, down.clocks).matched


@pytest.mark.parametrize("indicator", list(CATALOG))
def test_indicator_has_warmup_and_finite_output(indicator):
    rng = random.Random(50)
    values = []
    v = 100
    for _ in range(250):
        v += rng.uniform(-0.8, 0.8)
        values.append(v)
    b = bars(values)
    data = series(indicator, b, 14)
    assert len(data) == len(b)
    assert data[-1] is not None
    assert all(x is None or math.isfinite(x) for x in data)


def test_known_rsi_and_sma():
    assert series("RSI", bars(list(range(10, 50))), 14)[-1] == 100
    assert series("RSI", bars([100] * 50), 14)[-1] == 50
    assert series("SMA", bars([10, 20, 30]), 3)[-1] == 20


def test_depth_consumption_and_minimums():
    snapshot = book(size=0.1)
    settings = config()["settings"]
    settings["execution_policy"] = "fok"
    with pytest.raises(ExecutionRejected):
        PaperExecutor(snapshot).estimate("LONG", "ENTRY", settings, notional=100)
    before = deepcopy(snapshot)
    with pytest.raises(ExecutionRejected):
        PaperExecutor(snapshot).estimate("LONG", "ENTRY", settings, notional=0.00001)
    assert snapshot == before
    f = PaperExecutor(snapshot).estimate("LONG", "ENTRY", settings, notional=5)
    assert 0 < f.notional <= 5
    assert snapshot["asks"][0][1] < 0.1


@pytest.mark.parametrize("cycle", range(10))
def test_ten_independent_full_cycles_conserve_cash(cycle):
    cfg = config()
    s = cfg["settings"]
    s["orders"]["count"] = 3
    state = initial_state(500)
    events = []
    symbol = "BTCUSDT"
    key = "BTCUSDT:test"
    orders = plan(s["orders"], "LONG", 100)
    attach(state, price=100)
    assert enter(state, events, key, symbol, "LONG", s, 100, 25, orders, "test")[0]
    assert reserved(state) == pytest.approx(25, abs=0.01)
    attach(state, price=95)
    assert add_order(state, events, key, 95)
    attach(state, price=110 + cycle)
    assert exit_position(state, events, key, 110 + cycle, "TEST")
    assert not state["positions"]
    assert reserved(state) == 0
    assert state["cash"] == pytest.approx(500 + sum(e["cash_delta"] for e in events))
    assert state["stats"][key]["cycles"] == 1
    assert state["stats"][key]["net_pnl"] == pytest.approx(
        sum(e["cash_delta"] for e in events)
    )


def test_no_liquidity_means_no_phantom_position_or_lost_exit():
    cfg = config()
    s = cfg["settings"]
    state = initial_state(500)
    events = []
    orders = plan(s["orders"], "LONG", 100)
    assert not enter(state, events, "x", "BTCUSDT", "LONG", s, 100, 25, orders, "x")[0]
    assert state["cash"] == 500
    attach(state)
    assert enter(state, events, "x", "BTCUSDT", "LONG", s, 100, 25, orders, "x")[0]
    state["_books"] = {}
    assert not exit_position(state, events, "x", 100, "TEST")
    assert state["positions"]["x"]["exit_pending"]["reason"] == "TEST"
    attach(state)
    assert exit_position(state, events, "x", 100, "TEST")


def test_partial_profit_releases_margin_and_cash_is_conserved():
    cfg = config()
    s = cfg["settings"]
    state = initial_state(500)
    events = []
    attach(state)
    assert enter(
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
    )[0]
    qty = state["positions"]["x"]["qty"]
    attach(state, price=105)
    exit_position(state, events, "x", 105, "PART", 0.5)
    assert state["positions"]["x"]["qty"] < qty
    attach(state, price=110)
    exit_position(state, events, "x", 110, "FINISH")
    assert state["cash"] == pytest.approx(500 + sum(e["cash_delta"] for e in events))


def test_sqlite_rollback_and_restart_keep_open_position(tmp_path):
    store = Store(tmp_path, 500)
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
        state.pop("_books")
    original = store.read()
    with pytest.raises(RuntimeError):
        with store.transaction() as (state, events):
            state["cash"] = 0
            raise RuntimeError("crash")
    assert Store(tmp_path).read() == original
    assert len(store.history()) == 1


def test_repository_copy_edit_and_lock(tmp_path):
    repo = Repository(tmp_path)
    cfg = config()
    folder = repo.create(cfg)
    with BotLock(folder):
        with pytest.raises(BotBusy):
            repo.copy(folder)
    cfg, digest = repo.load(folder)
    cfg["name"] = "Edited"
    repo.save(folder, cfg, digest)
    with pytest.raises(RuntimeError):
        repo.save(folder, cfg, digest)
    copy = repo.copy(folder)
    assert repo.load(copy)[0]["name"] == "Edited — копия"
    repo.delete(copy)
    assert not copy.exists()


def test_shared_boundary_closes_then_opens_next_section():
    cfg = config()
    cfg["symbols"] = ["BTCUSDT"]
    cfg["sections"] = [range_section(30, 40), range_section(40, 50)]
    for sec, entry, exit_ in [
        (cfg["sections"][0], 100, 110),
        (cfg["sections"][1], 110, 120),
    ]:
        sec["entry"] = group(condition("PRICE", entry, "cross_up", 1))
        sec["exit"] = group(condition("PRICE", exit_, "cross_up", 1))
    cfg["settings"]["orders"]["count"] = 1
    state = initial_state(500)
    events = []
    engine = Veles()
    attach(state, price=101)
    engine.process(
        cfg, state, events, "BTCUSDT", bars([99, 101]), 100.99, 101.01, 120000
    )
    assert len(state["positions"]) == 1
    attach(state, price=111)
    engine.process(
        cfg, state, events, "BTCUSDT", bars([99, 101, 111]), 110.99, 111.01, 180000
    )
    assert list(state["positions"]) == ["BTCUSDT:LONG_40_50"]
    assert [e["action"] for e in events] == ["OPEN", "CLOSE", "OPEN"]


def test_independent_two_minute_entry_and_one_minute_exit():
    cfg = config()
    cfg["symbols"] = ["BTCUSDT"]
    sec = range_section()
    sec["entry"] = group(condition("PRICE", 100, "cross_up", 2))
    sec["exit"] = group(condition("PRICE", 110, "cross_up", 1))
    cfg["sections"] = [sec]
    state = initial_state(500)
    events = []
    engine = Veles()
    values = [99, 99, 101, 101]
    attach(state, price=101)
    engine.process(
        cfg, state, events, "BTCUSDT", bars(values), 100.99, 101.01, 4 * 60000
    )
    assert len(state["positions"]) == 1
    values.append(111)
    attach(state, price=111)
    engine.process(
        cfg, state, events, "BTCUSDT", bars(values), 110.99, 111.01, 5 * 60000
    )
    assert not state["positions"]
    # The same closed 2m entry candle is not consumed again by the 1m exit.
    assert [e["action"] for e in events] == ["OPEN", "CLOSE"]


def test_transfer_preserves_history_and_rejects_bad_archive(tmp_path):
    import zipfile

    from bcc.transfer import export_bots, import_bots

    repo = Repository(tmp_path / "app")
    folder = repo.create(config())
    Store(folder, 500)
    archive = tmp_path / "bots.zip"
    export_bots(repo, [folder], archive)
    targets = import_bots(repo, archive)
    assert Store(targets[0]).read()["cash"] == 500
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("../escape", "bad")
    with pytest.raises(ValueError):
        import_bots(repo, archive)


@pytest.mark.parametrize("removed", ["coin", "section"])
def test_edit_cannot_orphan_a_pending_grid(tmp_path, removed):
    cfg = config()
    cfg["symbols"] = ["BTCUSDT"]
    cfg["sections"] = cfg["sections"][:1]
    cfg["sections"][0]["entry"] = group(condition("PRICE", 100, "cross_up", 1))
    cfg["settings"]["orders"]["first_offset_pct"] = 1
    repo = Repository(tmp_path)
    folder = repo.create(cfg)
    saved, digest = repo.load(folder)
    store = Store(folder, 500)
    with store.transaction() as (state, events):
        attach(state, price=101)
        Veles().process(
            cfg, state, events, "BTCUSDT", bars([99, 101]), 100.99, 101.01, 120000
        )
        assert state["pending"] and not state["positions"]
        state.pop("_books")
    if removed == "coin":
        saved["symbols"] = ["ETHUSDT"]
    else:
        saved["sections"][0]["id"] = "another_section"
    with pytest.raises(ValueError, match="ожидающая сетка"):
        repo.save(folder, saved, digest)
    assert repo.load(folder)[1] == digest


@pytest.mark.parametrize("field", ["apiKey", "private_key", "access-token", "password"])
def test_paper_configuration_cannot_store_access_credentials(field):
    cfg = config()
    cfg["sections"][0]["entry"]["conditions"][0][field] = "sensitive-value"
    with pytest.raises(ValueError, match="ключи доступа"):
        validate(cfg)
