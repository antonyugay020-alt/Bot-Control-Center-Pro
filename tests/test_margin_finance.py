"""Public margin inputs reach accounting, liquidation and durable recovery."""

from copy import deepcopy
import time

import pytest

from bcc.config import defaults
from bcc.execution import PaperExecutor
from bcc.margin import (
    MODEL,
    MarginUnavailable,
    close_fee,
    effective_value,
    entry_reason,
    parse_tiers,
    requirements,
    snapshot,
    tier_for,
)
from bcc.market_clock import observed_quote
from bcc.storage import Store, initial_state
from bcc.trading import (
    available_margin,
    enter,
    equity,
    liquidations,
    plan,
    reserved,
    add_order,
)
from test_core import attach, book


def raw_tiers(symbol="BTCUSDT"):
    # Bybit's published maintenance-margin illustration, not current market rates.
    return [
        dict(
            id=i,
            symbol=symbol,
            riskLimitValue=str(limit),
            maintenanceMargin=rate,
            initialMargin=1 / lev,
            maxLeverage=str(lev),
            mmDeduction=str(deduction),
        )
        for i, limit, rate, lev, deduction in [
            (1, 100_000, 0.02, 25, 0),
            (2, 200_000, 0.025, 20, 500),
            (3, 300_000, 0.03, 16, 1500),
            (4, 400_000, 0.035, 14, 3000),
            (5, 500_000, 0.04, 12, 5000),
        ]
    ]


def use_mark(state, mark=100, symbol="BTCUSDT", tiers=None):
    now = time.time()
    state["margin_model"] = MODEL
    data = dict(
        symbol=symbol,
        tiers=tiers or parse_tiers(raw_tiers(symbol), symbol),
        risk_at=now,
        quote=observed_quote(dict(bid=mark, ask=mark, at=now)),
    )
    state.setdefault("margin_data", {})[symbol] = data
    return data


def opened(
    *, side="LONG", mode="ISOLATED", capital=500, margin=25, count=1, key="BTCUSDT:main"
):
    state, events = initial_state(capital), []
    settings = defaults()["settings"]
    settings.update(leverage=10, margin_mode=mode, slippage_pct=0)
    settings["orders"]["count"] = count
    attach(state)
    ok, why = enter(
        state,
        events,
        key,
        "BTCUSDT",
        side,
        settings,
        100,
        margin,
        plan(settings["orders"], side, 100),
        "main",
    )
    assert ok, why
    state["marks"]["BTCUSDT"] = observed_quote(
        dict(bid=99.99, ask=100.01, at=time.time())
    )
    use_mark(state)
    return state, events, settings


def test_official_maintenance_example_and_boundary_continuity():
    tiers = parse_tiers(raw_tiers(), "BTCUSDT")
    tier = tier_for(tiers, 400_000)
    assert 400_000 * tier["maintenance_rate"] - tier["deduction"] == pytest.approx(
        11_000
    )
    for i, value in enumerate([100_000, 200_000, 300_000, 400_000]):
        before = value * tiers[i]["maintenance_rate"] - tiers[i]["deduction"]
        after = value * tiers[i + 1]["maintenance_rate"] - tiers[i + 1]["deduction"]
        assert before == pytest.approx(after)
    assert tier_for(tiers, 100_000)["id"] == 1
    assert tier_for(tiers, 100_000.01)["id"] == 2
    with pytest.raises(MarginUnavailable):
        tier_for(tiers, 500_001)


def test_empty_deduction_is_derived_from_confirmed_adjacent_tiers():
    rows = raw_tiers()
    for row in rows:
        row["mmDeduction"] = ""
    assert [t["deduction"] for t in parse_tiers(rows, "BTCUSDT")] == pytest.approx(
        [0, 500, 1500, 3000, 5000]
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("maintenanceMargin", "nan"),
        ("initialMargin", "inf"),
        ("riskLimitValue", -1),
        ("maintenanceMargin", 0.5),
        ("mmDeduction", 100),
        ("symbol", "ETHUSDT"),
    ],
)
def test_bad_public_tier_cannot_be_used(key, value):
    rows = raw_tiers()
    rows[0][key] = value
    with pytest.raises(MarginUnavailable):
        parse_tiers(rows, "BTCUSDT")


def test_deduction_is_allocated_once_across_account_sections():
    state, _, _ = opened()
    first = state["positions"]["BTCUSDT:main"]
    first.update(qty=1500, average=100, margin=15000)
    state["positions"]["BTCUSDT:second"] = deepcopy(first)
    one, two = [requirements(state, p) for p in state["positions"].values()]
    assert one["deduction"] + two["deduction"] == pytest.approx(1500)
    assert one["maintenance"] + two["maintenance"] == pytest.approx(7500)


@pytest.mark.parametrize("side,mark", [("LONG", 90), ("SHORT", 110)])
def test_mark_triggers_isolated_liquidation_even_when_book_looks_safe(side, mark):
    state, events, _ = opened(side=side)
    use_mark(state, mark)
    liquidations(state, events)
    assert not state["positions"]
    closed = [e for e in events if e["action"] == "CLOSE"]
    assert closed[0]["reason"] == "PAPER_LIQUIDATION"
    assert closed[0]["margin_evidence"]["mark_price"] == mark
    assert closed[0]["price"] != mark  # execution uses available depth


def test_spread_spike_does_not_substitute_for_mark_in_liquidation_or_equity():
    state, events, _ = opened()
    attach(state, price=80)
    state["marks"]["BTCUSDT"] = observed_quote(
        dict(bid=79.99, ask=80.01, at=time.time())
    )
    liquidations(state, events)
    assert state["positions"]
    p = next(iter(state["positions"].values()))
    assert equity(state)[0] == pytest.approx(
        state["cash"] + (100 - p["average"]) * p["qty"]
    )
    assert not [e for e in events if e["action"] == "CLOSE"]


@pytest.mark.parametrize(
    "fault", ["missing", "stale_mark", "stale_tiers", "wrong_symbol"]
)
def test_missing_fresh_margin_data_never_uses_guessed_liquidation(fault):
    state, events, settings = opened()
    data = use_mark(state, 80)
    if fault == "missing":
        state.pop("margin_data")
    if fault == "stale_mark":
        data["quote"]["at"] -= 10
        data["quote"]["source_age_high"] += 10
    if fault == "stale_tiers":
        data["risk_at"] -= 400
    if fault == "wrong_symbol":
        data["symbol"] = "ETHUSDT"
    liquidations(state, events)
    assert state["positions"] and len(events) == 1
    assert entry_reason(
        state, "BTCUSDT", "LONG", settings, 1, plan(settings["orders"], "LONG", 100)
    )
    assert next(iter(state["positions"].values()))["margin_data_error"]


def test_cross_initial_margin_changes_with_mark_and_close_fee_is_reserved():
    state, _, _ = opened(mode="CROSS")
    p = next(iter(state["positions"].values()))
    original = reserved(state)
    use_mark(state, 110)
    expected = p["qty"] * 110 / p["settings"]["leverage"] + close_fee(p)
    assert reserved(state) == pytest.approx(expected)
    assert reserved(state) > original
    assert available_margin(state) == pytest.approx(equity(state)[0] - expected)


def test_stale_second_cross_mark_blocks_portfolio_liquidation():
    state, events, _ = opened(mode="CROSS", capital=30)
    p = next(iter(state["positions"].values()))
    other = deepcopy(p)
    other["symbol"] = "ETHUSDT"
    state["positions"]["ETHUSDT:main"] = other
    use_mark(state, 50)
    liquidations(state, events)
    assert len(state["positions"]) == 2
    assert not [e for e in events if e["action"] == "CLOSE"]


def test_isolated_gap_cannot_consume_other_sections_collateral(tmp_path):
    state, events, _ = opened()
    original_fee = events[0]["fee"]
    allocated = next(iter(state["positions"].values()))["margin"]
    attach(state, price=1)
    state["marks"]["BTCUSDT"] = observed_quote(dict(bid=0.99, ask=1.01, at=time.time()))
    use_mark(state, 1)
    liquidations(state, events)
    assert state["cash"] == pytest.approx(500 - original_fee - allocated)
    close = events[-1]
    assert close["paper_isolated_loss_coverage"] > 0
    assert close["execution_cash_delta"] < -25
    store = Store(tmp_path, 500)
    with store.transaction() as (saved, executions):
        saved.update(state)
        executions.extend(events)
    store.audit()
    assert Store(tmp_path).read()["cash"] == pytest.approx(state["cash"])


def test_expired_mark_after_reopen_preserves_history_until_fresh_data(tmp_path):
    state, events, _ = opened()
    state.pop("_books")
    store = Store(tmp_path, 500)
    with store.transaction() as (saved, executions):
        saved.update(state)
        executions.extend(events)
    before = store.read()["positions"]
    with Store(tmp_path).transaction() as (saved, executions):
        saved["margin_data"]["BTCUSDT"]["quote"]["source_age_high"] = 20
        attach(saved, price=80)
        liquidations(saved, executions)
    assert Store(tmp_path).read()["positions"].keys() == before.keys()
    assert len(store.history()) == 1
    with Store(tmp_path).transaction() as (saved, executions):
        use_mark(saved, 80)
        attach(saved, price=80)
        liquidations(saved, executions)
    store.audit()
    assert not store.read()["positions"]
    assert [e["action"] for e in reversed(store.history())] == ["OPEN", "CLOSE"]


def test_preview_never_consumes_liquidity_in_ioc_and_budget_recursion():
    snapshot_book = book(size=0.1)
    settings = defaults()["settings"]
    executor = PaperExecutor(snapshot_book)
    before = deepcopy(snapshot_book)
    preview = executor.estimate("LONG", "ENTRY", settings, notional=100, consume=False)
    assert preview.partial
    assert snapshot_book == before
    fill = executor.estimate("LONG", "ENTRY", settings, notional=100)
    assert fill.quantity == preview.quantity and fill.price == preview.price
    assert sum(size for price, size in snapshot_book["asks"]) == 0


def test_actual_depth_margin_check_rejects_before_consuming_liquidity():
    state, events = initial_state(25.16), []
    settings = defaults()["settings"]
    settings.update(leverage=10, slippage_pct=0, fee_rate=0.0006)
    settings["orders"]["count"] = 1
    use_mark(state, 95)
    attach(state)
    before = deepcopy(state["_books"])
    ok, reason = enter(
        state,
        events,
        "BTCUSDT:main",
        "BTCUSDT",
        "LONG",
        settings,
        100,
        25,
        plan(settings["orders"], "LONG", 100),
        "main",
    )
    assert not ok and "маржи" in reason.lower()
    assert not state["positions"] and not events
    assert state["_books"] == before


def test_dca_reserves_continue_after_store_reopen_and_keep_ledger(tmp_path):
    state, events, _ = opened(count=2)
    store = Store(tmp_path, 500)
    with store.transaction() as (saved, executions):
        saved.update(state)
        executions.extend(events)
    with Store(tmp_path).transaction() as (saved, executions):
        use_mark(saved, 90)
        attach(saved, price=89.99)
        assert add_order(saved, executions, "BTCUSDT:main", 90)
    store.audit()
    p = next(iter(store.read()["positions"].values()))
    assert p["dca_fills"] == 1 and p["order_margin"] == 0
    assert [e["action"] for e in reversed(store.history())] == ["OPEN", "DCA"]


def test_smaller_hedge_does_not_receive_the_larger_sides_margin_deduction():
    state, _, _ = opened()
    long = next(iter(state["positions"].values()))
    long.update(qty=4000, average=100, margin=40000)
    short = deepcopy(long)
    short.update(qty=1, side="SHORT", margin=10)
    state["positions"]["BTCUSDT:short"] = short
    short_mm = requirements(state, short)
    assert short_mm["tier_id"] == 1
    assert short_mm["deduction"] == 0
    assert short_mm["maintenance"] == 2


def public_source(monkeypatch, *, mark=100, book_price=100, broken_risk=False):
    from bcc.market import PublicData, DataUnavailable
    import bcc.market as market_module

    source = PublicData()
    calls = []
    monkeypatch.setattr(market_module.clock, "ensure", lambda: None)

    def get(url, params=None, ttl=0):
        calls.append(url.rsplit("/", 1)[-1])
        now = time.time()
        if url.endswith("instruments-info"):
            return dict(
                result=dict(
                    list=[
                        dict(
                            symbol="BTCUSDT",
                            status="Trading",
                            contractType="LinearPerpetual",
                            quoteCoin="USDT",
                            lotSizeFilter=dict(
                                qtyStep=".0001",
                                minOrderQty=".0001",
                                minNotionalValue=".01",
                                maxMktOrderQty="1000000",
                            ),
                            priceFilter=dict(tickSize=".01"),
                        )
                    ]
                )
            )
        if url.endswith("risk-limit"):
            if broken_risk:
                raise DataUnavailable("Risk-limit unavailable")
            return dict(
                retCode=0,
                time=now * 1000,
                result=dict(list=raw_tiers(), nextPageCursor=""),
            )
        if url.endswith("tickers"):
            return dict(
                retCode=0,
                time=now * 1000,
                result=dict(list=[dict(symbol="BTCUSDT", markPrice=str(mark))]),
            )
        if url.endswith("orderbook"):
            return dict(
                retCode=0,
                time=now * 1000,
                result=dict(
                    s="BTCUSDT",
                    ts=now * 1000,
                    b=[[str(book_price - 0.01), "1000000"]],
                    a=[[str(book_price + 0.01), "1000000"]],
                ),
            )
        raise AssertionError(url)

    monkeypatch.setattr(source, "get", get)
    return source, calls


def runtime_config():
    from bcc.config import condition, group

    cfg = defaults()
    cfg.update(name="Public margin runtime", symbols=["BTCUSDT"])
    cfg["sections"] = cfg["sections"][:1]
    cfg["sections"][0]["entry"] = group(condition("PRICE", 1, ">", 1))
    cfg["settings"]["orders"]["count"] = 1
    cfg["settings"].update(margin_mode="ISOLATED", leverage=10)
    cfg["settings"]["profit"].update(mode="simple", pct=1)
    return cfg


def tick_with(book):
    from bcc.scheduler import MarketTick
    from test_core import bars

    base = int(time.time()) // 60 * 60000 - 180000
    return MarketTick("BTCUSDT", bars([100, 100], base), book, [], {})


def test_public_data_orderbook_and_runtime_use_public_margin_without_editing_config(
    tmp_path, monkeypatch
):
    from bcc.runtime import process_tick
    from bcc.strategies import Veles

    cfg = runtime_config()
    before = deepcopy(cfg)
    source, calls = public_source(monkeypatch)
    try:
        executable = source.orderbook("BTCUSDT")
    finally:
        source.close()
    assert calls == ["instruments-info", "risk-limit", "tickers", "orderbook"]
    assert executable["margin_required"] and executable["margin_data"]["tiers"]
    store = Store(tmp_path, 500)
    process_tick(cfg, store, Veles(), tick_with(executable))
    saved = store.read()
    assert saved["margin_model"] == MODEL
    assert saved["positions"]
    assert (
        next(iter(saved["positions"].values()))["margin_evidence"]["mark_price"] == 100
    )
    assert cfg == before
    store.audit()


def test_public_margin_endpoint_failure_keeps_fresh_depth_but_blocks_new_entry(
    tmp_path, monkeypatch
):
    from bcc.runtime import process_tick
    from bcc.strategies import Veles

    source, calls = public_source(monkeypatch, broken_risk=True)
    try:
        executable = source.orderbook("BTCUSDT")
    finally:
        source.close()
    assert calls[-1] == "orderbook" and executable["bids"]
    assert executable["margin_error"] == "Risk-limit unavailable"
    store = Store(tmp_path, 500)
    process_tick(runtime_config(), store, Veles(), tick_with(executable))
    assert not store.read()["positions"] and not store.history()
    reasons = [row["reason"] for row in store.read()["diagnostics"]["BTCUSDT"]]
    assert any("марж" in reason.lower() or "Risk-limit" in reason for reason in reasons)


def test_expired_margin_does_not_block_take_profit_using_fresh_depth(
    tmp_path, monkeypatch
):
    from bcc.runtime import process_tick
    from bcc.strategies import Veles

    cfg = runtime_config()
    store = Store(tmp_path, 500)
    source, _ = public_source(monkeypatch)
    try:
        process_tick(cfg, store, Veles(), tick_with(source.orderbook("BTCUSDT")))
    finally:
        source.close()
    assert store.read()["positions"]
    with store.transaction() as (state, events):
        state["margin_data"]["BTCUSDT"]["quote"]["source_age_high"] = 20
    source, _ = public_source(monkeypatch, book_price=110, broken_risk=True)
    try:
        process_tick(cfg, store, Veles(), tick_with(source.orderbook("BTCUSDT")))
    finally:
        source.close()
    assert not store.read()["positions"]
    assert [e["action"] for e in reversed(store.history())] == ["OPEN", "CLOSE"]
    assert store.history()[0]["reason"] == "TAKE_PROFIT"
    store.audit()


def test_invalid_fractional_tier_id_is_rejected():
    rows = raw_tiers()
    rows[0]["id"] = 1.5
    with pytest.raises(MarginUnavailable):
        parse_tiers(rows, "BTCUSDT")


def test_idle_scan_prunes_only_unused_margin_data_and_keeps_exposed_symbols():
    from bcc.margin import attach_snapshot

    state = initial_state(500)
    for symbol in ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]:
        use_mark(state, 100, symbol)
    state["positions"] = {"a": dict(symbol="BTCUSDT")}
    state["pending"] = {"b": dict(symbol="ETHUSDT")}
    attach_snapshot(state, "SOLUSDT", dict(margin_required=True))
    assert set(state["margin_data"]) == {"BTCUSDT", "ETHUSDT", "SOLUSDT"}


def test_signal_dca_is_not_counted_as_an_already_active_exchange_order():
    state, _, _ = opened(count=4)
    p = next(iter(state["positions"].values()))
    p["settings"]["orders"]["mode"] = "signal"
    sides = effective_value(state, "BTCUSDT", 100)
    assert sides["LONG"] == pytest.approx(p["qty"] * 100)


def test_dca_admission_includes_newly_activated_grid_levels():
    from bcc.margin import dca_extra_value

    state, _, _ = opened(count=5)
    p = next(iter(state["positions"].values()))
    p["settings"]["orders"]["partial_count"] = 1
    p["active_until"] = 2
    mark = 90
    use_mark(state, mark)
    attach(state, price=89.99)
    amount = p["budget"] * p["orders"][1]["weight"]
    preview = PaperExecutor(state["_books"]["BTCUSDT"]).estimate(
        "LONG",
        "ENTRY",
        p["settings"],
        notional=amount,
        limit=p["orders"][1]["price"],
        consume=False,
    )
    old_order = amount / p["orders"][1]["price"] * mark
    added_level = (
        p["budget"] * p["orders"][2]["weight"] / p["orders"][2]["price"] * mark
    )
    assert dca_extra_value(state, p, preview) == pytest.approx(
        preview.quantity * mark - old_order + added_level
    )
