"""Risk-reduction stages execute real PAPER depth and persist absolute targets."""

from copy import deepcopy
import time

import pytest

from bcc.config import defaults
from bcc.margin import MODEL, parse_tiers
from bcc.market_clock import observed_quote
from bcc.storage import AccountingError, Store, initial_state
from bcc.trading import enter, exit_position, liquidations, plan, add_order
from test_core import attach, book
from test_margin_finance import use_mark


def tiers(symbol="BTCUSDT"):
    return parse_tiers(
        [
            dict(
                id=1,
                symbol=symbol,
                riskLimitValue="100",
                maintenanceMargin=0.05,
                initialMargin=0.25,
                maxLeverage="4",
                mmDeduction="0",
            ),
            dict(
                id=2,
                symbol=symbol,
                riskLimitValue="200",
                maintenanceMargin=0.10,
                initialMargin=0.4,
                maxLeverage="2.5",
                mmDeduction="5",
            ),
            dict(
                id=3,
                symbol=symbol,
                riskLimitValue="400",
                maintenanceMargin=0.20,
                initialMargin=0.5,
                maxLeverage="2",
                mmDeduction="25",
            ),
        ],
        symbol,
    )


def opened(mode="ISOLATED", *, count=1, capital=200):
    state, events = initial_state(capital), []
    settings = defaults()["settings"]
    settings.update(
        leverage=2.5,
        margin_mode=mode,
        slippage_pct=0,
        fee_rate=0,
        execution_policy="fok",
    )
    settings["orders"]["count"] = count
    attach(state, price=180)
    ok, why = enter(
        state,
        events,
        "BTCUSDT:main",
        "BTCUSDT",
        "SHORT",
        settings,
        179.99,
        72,
        plan(settings["orders"], "SHORT", 180),
        "main",
    )
    assert ok, why
    tick(state, 180)
    return state, events


def tick(state, mark, *, size=10000, maximum=None):
    state.pop("_entry_blocked", None)
    use_mark(state, mark, tiers=tiers())
    snapshot = book(mark, size)
    if maximum is not None:
        snapshot["max_qty"] = maximum
    state["_books"] = {"BTCUSDT": snapshot}
    state["marks"]["BTCUSDT"] = observed_quote(
        dict(bid=mark - 0.01, ask=mark + 0.01, at=time.time())
    )


def fills(events):
    return [e for e in events if e["action"] in ("PARTIAL", "CLOSE")]


def save_initial(folder, state, events):
    store = Store(folder, state["initial"])
    with store.transaction() as (saved, executions):
        saved.update(state)
        executions.extend(events)
    return store


def test_one_tier_reduction_saves_isolated_position_and_leaves_configuration_intact():
    state, events = opened()
    before = deepcopy(state["positions"]["BTCUSDT:main"]["settings"])
    tick(state, 232)
    liquidations(state, events)
    p = state["positions"]["BTCUSDT:main"]
    assert p["qty"] == pytest.approx(0.862)
    assert not p.get("liquidation") and not p.get("exit_pending")
    assert p["settings"] == before
    assert p["last_liquidation"]["stage"] == "RECOVERED"
    assert fills(events)[0]["liquidation"]["from_tier"] == 3
    assert fills(events)[0]["liquidation"]["to_tier"] == 2
    assert fills(events)[0]["liquidation"]["target_remaining_qty"] == pytest.approx(
        0.862
    )
    assert fills(events)[0]["qty"] == pytest.approx(0.138)
    assert events[-1]["action"] == "LIQUIDATION_RECOVERED"
    assert not state["stats"]  # a reduction is not a completed trading cycle


def test_partial_ioc_keeps_absolute_target_across_reopen(tmp_path):
    state, events = opened()
    tick(state, 232, size=0.01)  # two ask levels provide 0.02, despite bot FOK
    liquidations(state, events)
    p = state["positions"]["BTCUSDT:main"]
    target = p["liquidation"]["target_remaining_qty"]
    operation_id = p["liquidation"]["id"]
    assert p["qty"] == pytest.approx(0.98)
    assert p["exit_pending"]["execution_policy"] == "ioc"
    store = save_initial(tmp_path, state, events)
    with Store(tmp_path).transaction() as (saved, executions):
        tick(saved, 232)
        liquidations(saved, executions)
    result = store.read()["positions"]["BTCUSDT:main"]
    assert result["qty"] == pytest.approx(target)
    reductions = fills(list(reversed(store.history())))
    assert sum(e["qty"] for e in reductions) == pytest.approx(1 - target)
    assert {e["liquidation"]["id"] for e in reductions} == {operation_id}
    store.audit()


def test_recovered_market_cancels_unfilled_reduction_without_selling_again():
    state, events = opened()
    tick(state, 232, size=0)
    liquidations(state, events)
    assert state["positions"]["BTCUSDT:main"]["liquidation"]["stage"] == "REDUCE"
    assert not fills(events)
    tick(state, 180)
    liquidations(state, events)
    p = state["positions"]["BTCUSDT:main"]
    assert p["qty"] == pytest.approx(1)
    assert not p.get("liquidation") and not p.get("exit_pending")
    assert not fills(events)
    assert events[-1]["action"] == "LIQUIDATION_RECOVERED"


def test_failed_recheck_escalates_to_full_exit_after_reduction(tmp_path):
    state, events = opened()
    tick(state, 235)
    liquidations(state, events)
    p = state["positions"]["BTCUSDT:main"]
    assert p["liquidation"]["stage"] == "RECHECK"
    assert p["qty"] == pytest.approx(0.851)
    store = save_initial(tmp_path, state, events)
    with store.transaction() as (saved, executions):
        tick(saved, 235)
        liquidations(saved, executions)
    assert not store.read()["positions"]
    store.audit()
    rows = fills(list(reversed(store.history())))
    assert [e["action"] for e in rows] == ["PARTIAL", "CLOSE"]
    assert rows[-1]["liquidation"]["stage"] == "FULL"
    assert sum(e["qty"] for e in rows) == pytest.approx(1)
    assert store.read()["stats"]["BTCUSDT:main"]["cycles"] == 1


def test_each_physical_request_respects_maximum_order_quantity():
    state, events = opened()
    tick(state, 235, maximum=0.05)
    for _ in range(25):
        liquidations(state, events)
        if not state["positions"]:
            break
        tick(state, 235, maximum=0.05)
    assert not state["positions"]
    assert all(e["qty"] <= 0.05 + 1e-10 for e in fills(events))
    assert sum(e["qty"] for e in fills(events)) == pytest.approx(1)
    assert len({e["liquidation"]["id"] for e in fills(events)}) == 1


def test_missing_mark_does_not_guess_next_stage_after_partial_fill():
    state, events = opened()
    tick(state, 232, size=0.01)
    liquidations(state, events)
    before = state["positions"]["BTCUSDT:main"]["qty"]
    state["margin_data"]["BTCUSDT"]["quote"]["source_age_high"] = 20
    state["_books"] = {"BTCUSDT": book(232)}
    liquidations(state, events)
    assert state["positions"]["BTCUSDT:main"]["qty"] == before
    assert "mark-price" in state["positions"]["BTCUSDT:main"]["liquidation_error"]


def test_cancelled_future_dca_is_not_restored_after_risk_recovers():
    state, events = opened()
    p = state["positions"]["BTCUSDT:main"]
    # Existing accepted orders do not alter the filled position or its margin.
    p["orders"].append(dict(price=260, weight=0.1, anchor=180))
    p["order_margin"] = 7.2
    tick(state, 232, size=0.01)
    liquidations(state, events)
    assert p["order_margin"] == 0 and p["next_order"] == len(p["orders"])
    first_count = sum(e["action"] == "CANCEL_GRID" for e in events)
    tick(state, 180)
    liquidations(state, events)
    assert p["order_margin"] == 0
    assert sum(e["action"] == "CANCEL_GRID" for e in events) == first_count == 1


def test_cross_recalculates_after_one_reduction_and_preserves_other_symbol():
    state, events = opened(mode="CROSS", capital=76.1)
    # Set up the independent section using the existing execution contract.
    state.pop("margin_model")
    p = state["positions"]["BTCUSDT:main"]
    settings = deepcopy(p["settings"])
    attach(state, symbol="ETHUSDT", price=100)
    ok, why = enter(
        state,
        events,
        "ETHUSDT:main",
        "ETHUSDT",
        "LONG",
        settings,
        100.01,
        4,
        plan(settings["orders"], "LONG", 100),
        "other",
    )
    assert ok, why
    other_qty = state["positions"]["ETHUSDT:main"]["qty"]
    tick(state, 234)
    use_mark(state, 100.01, "ETHUSDT", tiers("ETHUSDT"))
    liquidations(state, events)
    assert set(state["positions"]) == {"BTCUSDT:main", "ETHUSDT:main"}
    assert state["positions"]["ETHUSDT:main"]["qty"] == other_qty
    assert fills(events) and all(e["symbol"] == "BTCUSDT" for e in fills(events))
    assert not any(p.get("liquidation") for p in state["positions"].values())


def test_cancelling_cross_orders_can_restore_collateral_without_closing_positions():
    state, events = initial_state(100), []
    settings = defaults()["settings"]
    settings.update(leverage=2, margin_mode="CROSS", fee_rate=0, slippage_pct=0)
    settings["orders"]["count"] = 1
    attach(state)
    ok, why = enter(
        state,
        events,
        "BTCUSDT:main",
        "BTCUSDT",
        "LONG",
        settings,
        100.01,
        25,
        plan(settings["orders"], "LONG", 100),
        "main",
    )
    assert ok, why
    table = parse_tiers(
        [
            dict(
                id=1,
                symbol="BTCUSDT",
                riskLimitValue="1000000",
                maintenanceMargin=0.4,
                initialMargin=0.5,
                maxLeverage="2",
                mmDeduction="0",
            )
        ],
        "BTCUSDT",
    )
    state["pending"]["BTCUSDT:pending"] = dict(
        symbol="BTCUSDT",
        side="LONG",
        margin=70,
        settings=deepcopy(settings),
        orders=[dict(price=100, weight=1)],
        section="pending",
    )
    use_mark(state, 200, tiers=table)
    qty = state["positions"]["BTCUSDT:main"]["qty"]
    liquidations(state, events)
    assert not state["pending"]
    assert state["positions"]["BTCUSDT:main"]["qty"] == qty
    assert not fills(events)
    assert events[-1]["reason"] == "CROSS_LIQUIDATION_RISK"


def test_liquidation_blocks_direct_entry_and_dca_without_changing_frozen_settings():
    state, events = opened(count=1)
    tick(state, 232, size=0)
    liquidations(state, events)
    p = state["positions"]["BTCUSDT:main"]
    before = deepcopy(p["settings"])
    state.pop("_entry_blocked", None)
    assert not add_order(state, events, "BTCUSDT:main", 232)
    ok, why = enter(
        state,
        events,
        "BTCUSDT:new",
        "BTCUSDT",
        "LONG",
        p["settings"],
        232,
        1,
        plan(p["settings"]["orders"], "LONG", 232),
        "new",
    )
    assert not ok and "ликвидац" in why.lower()
    assert p["settings"] == before


@pytest.mark.parametrize(
    "patch",
    [
        {"stage": "unknown"},
        {"target_remaining_qty": -1},
        {"target_remaining_qty": ".8"},
        {"initial_qty": 0.5},
        {"id": "wrong"},
    ],
)
def test_corrupted_reduction_state_is_rejected_instead_of_reset(patch):
    state, events = opened()
    tick(state, 232, size=0)
    liquidations(state, events)
    state["positions"]["BTCUSDT:main"]["liquidation"].update(patch)
    with pytest.raises(AccountingError):
        Store.validate_state(state)


def test_manual_full_exit_is_not_replaced_with_a_smaller_reduction():
    state, events = opened()
    tick(state, 232, size=0)
    exit_position(state, events, "BTCUSDT:main", 232, "MANUAL_PAPER_CLOSE")
    liquidations(state, events)
    p = state["positions"]["BTCUSDT:main"]
    assert p["liquidation"]["stage"] == "FULL"
    assert p["liquidation"]["target_remaining_qty"] == 0
    assert p["liquidation"]["preserved_full_exit"] == "MANUAL_PAPER_CLOSE"


def test_protective_exit_of_other_coin_continues_during_pending_risk_reduction(
    tmp_path,
):
    from bcc.config import condition, group
    from bcc.runtime import process_tick
    from bcc.scheduler import MarketTick
    from bcc.strategies import Veles
    from test_core import bars

    state, events = opened()
    state.pop("margin_model")
    settings = deepcopy(state["positions"]["BTCUSDT:main"]["settings"])
    settings["profit"].update(mode="simple", pct=1)
    attach(state, "ETHUSDT", 100)
    assert enter(
        state,
        events,
        "ETHUSDT:main",
        "ETHUSDT",
        "LONG",
        settings,
        100.01,
        10,
        plan(settings["orders"], "LONG", 100),
        "other",
    )[0]
    tick(state, 232, size=0)
    use_mark(state, 100.01, "ETHUSDT", tiers("ETHUSDT"))
    liquidations(state, events)
    assert state["positions"]["BTCUSDT:main"].get("liquidation")
    store = save_initial(tmp_path, state, events)
    cfg = defaults()
    cfg.update(name="Parallel protective exits", symbols=["BTCUSDT", "ETHUSDT"])
    cfg["sections"] = cfg["sections"][:1]
    cfg["sections"][0].update(id="main", entry=group(condition("PRICE", 1, ">", 1)))
    use_mark(state, 110, "ETHUSDT", tiers("ETHUSDT"))
    executable = book(110)
    executable.update(margin_required=True, margin_data=state["margin_data"]["ETHUSDT"])
    process_tick(
        cfg,
        store,
        Veles(),
        MarketTick(
            "ETHUSDT",
            bars([100, 110], int(time.time()) // 60 * 60000 - 180000),
            executable,
            [],
            {},
        ),
    )
    assert set(store.read()["positions"]) == {"BTCUSDT:main"}
    assert (
        store.history()[0]["action"] == "CLOSE"
        and store.history()[0]["symbol"] == "ETHUSDT"
    )
    store.audit()


def test_public_mark_batch_refreshes_cross_peer_without_persisting_the_entire_market(
    monkeypatch,
):
    from bcc.margin import attach_snapshot, snapshot
    from test_margin_finance import public_source

    state, events = opened(mode="CROSS")
    peer = deepcopy(state["positions"]["BTCUSDT:main"])
    peer["symbol"] = "ETHUSDT"
    state["positions"]["ETHUSDT:main"] = peer
    use_mark(state, 100, "ETHUSDT", tiers("ETHUSDT"))
    state["margin_data"]["ETHUSDT"]["quote"]["source_age_high"] = 20
    source, _ = public_source(monkeypatch)
    getter = source.get

    def get(url, params=None, ttl=0):
        response = getter(url, params, ttl)
        if url.endswith("tickers"):
            assert params == {"category": "linear"}
            response["result"]["list"].extend(
                [
                    dict(symbol="ETHUSDT", markPrice="101"),
                    dict(symbol="SOLUSDT", markPrice="200"),
                ]
            )
        return response

    monkeypatch.setattr(source, "get", get)
    try:
        executable = source.orderbook("BTCUSDT")
    finally:
        source.close()
    attach_snapshot(state, "BTCUSDT", executable)
    assert snapshot(state, "ETHUSDT")[1] == 101
    assert set(state["margin_data"]) == {"BTCUSDT", "ETHUSDT"}
    assert "account_marks" not in state["margin_data"]["BTCUSDT"]
    assert "SOLUSDT" in executable["margin_marks"]


def test_zero_quantity_manual_action_does_not_cancel_a_pending_risk_operation():
    state, events = opened()
    tick(state, 232, size=0)
    liquidations(state, events)
    original = deepcopy(state["positions"]["BTCUSDT:main"]["liquidation"])
    exit_position(state, events, "BTCUSDT:main", 232, "MANUAL_PAPER_CLOSE", ratio=0)
    assert state["positions"]["BTCUSDT:main"]["liquidation"] == original


def test_ioc_remainder_below_new_order_minimum_can_finish_full_liquidation():
    from test_margin_finance import opened as open_small

    state, events, _ = open_small()
    # Fresh market rules may raise the minimum after the position was opened.
    use_mark(state, 80)
    executable = book(80, size=0.05)
    executable["min_qty"] = 2.45
    state["_books"] = {"BTCUSDT": executable}
    liquidations(state, events)
    p = next(iter(state["positions"].values()))
    assert p["qty"] < 2.45 and p.get("liquidation")
    use_mark(state, 80)
    state["_books"] = {"BTCUSDT": book(80)}
    state["_books"]["BTCUSDT"]["min_qty"] = 2.45
    liquidations(state, events)
    assert not state["positions"]
    assert fills(events)[-1]["full_close_request"] is True
    assert sum(e["qty"] for e in fills(events)) == pytest.approx(events[0]["qty"])


def test_partial_exit_cannot_use_the_full_position_minimum_exception():
    from bcc.execution import ExecutionRejected, PaperExecutor

    snapshot = book()
    snapshot["min_qty"] = 1
    settings = defaults()["settings"]
    with pytest.raises(ExecutionRejected):
        PaperExecutor(snapshot).estimate(
            "LONG", "EXIT", settings, quantity=0.1, position_quantity=2
        )
    with pytest.raises(ExecutionRejected):
        PaperExecutor(snapshot).estimate(
            "LONG", "ENTRY", settings, quantity=0.1, position_quantity=0.1
        )
    with pytest.raises(ExecutionRejected):
        PaperExecutor(snapshot).estimate(
            "LONG", "EXIT", settings, quantity=3, position_quantity=2
        )


def test_complete_pending_operation_preserves_ledger_through_export_import(tmp_path):
    from bcc.storage import Repository
    from bcc.transfer import export_bots, import_bots

    repo = Repository(tmp_path / "app")
    cfg = defaults()
    cfg.update(name="Transfer pending reduction", capital=200, symbols=["BTCUSDT"])
    folder = repo.create(cfg)
    state, events = opened()
    tick(state, 232, size=0.01)
    liquidations(state, events)
    store = save_initial(folder, state, events)
    operation = store.read()["positions"]["BTCUSDT:main"]["liquidation"]["id"]
    archive = tmp_path / "transfer.zip"
    export_bots(repo, [folder], archive, history=True)
    copied = import_bots(repo, archive, history=True)[0]
    with Store(copied).transaction() as (saved, executions):
        tick(saved, 232)
        liquidations(saved, executions)
    Store(copied).audit()
    p = Store(copied).read()["positions"]["BTCUSDT:main"]
    assert p["last_liquidation"]["id"] == operation
    assert p["qty"] == pytest.approx(0.862)
    assert store.read()["positions"]["BTCUSDT:main"].get("liquidation")


@pytest.mark.parametrize("ratio", [1, 0.5])
def test_manual_exit_chunks_preserve_original_goal_through_store_reopen(
    tmp_path, ratio
):
    from decimal import Decimal
    from bcc.execution import floor_quantity
    from test_margin_finance import opened as normal_position

    state, events, _ = normal_position()
    initial = state["positions"]["BTCUSDT:main"]["qty"]
    goal = floor_quantity(initial * ratio, Decimal(".0001"))
    attach(state, price=100)
    state["_books"]["BTCUSDT"]["max_qty"] = 0.6
    exit_position(state, events, "BTCUSDT:main", 99.99, "MANUAL_PAPER_CLOSE", ratio)
    assert (
        state["positions"]["BTCUSDT:main"]["exit_pending"]["reason"]
        == "MANUAL_PAPER_CLOSE"
    )
    store = save_initial(tmp_path, state, events)
    for _ in range(8):
        with Store(tmp_path).transaction() as (saved, executions):
            p = saved["positions"].get("BTCUSDT:main")
            if not p or not p.get("exit_pending"):
                break
            pending = deepcopy(p["exit_pending"])
            attach(saved, price=100)
            saved["_books"]["BTCUSDT"]["max_qty"] = 0.6
            exit_position(
                saved,
                executions,
                "BTCUSDT:main",
                99.99,
                pending["reason"],
                pending["ratio"],
                limit=pending.get("limit"),
                target_index=pending.get("target_index"),
                execution_policy=pending.get("execution_policy"),
            )
    store.audit()
    actual = fills(list(reversed(store.history())))
    assert all(e["qty"] <= 0.6 + 1e-10 for e in actual)
    assert sum(e["qty"] for e in actual) == pytest.approx(goal)
    positions = store.read()["positions"]
    if ratio == 1:
        assert not positions
    else:
        assert positions["BTCUSDT:main"]["qty"] == pytest.approx(initial - goal)
        assert not positions["BTCUSDT:main"].get("exit_pending")
