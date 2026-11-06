"""Regression cases from the 20-pass audit; transports are the only fakes."""

import json
import os
import random
import time
from copy import deepcopy

import pytest
from test_core import attach, bars, book, config
from test_execution_runtime import open_position
from test_process import RUNNER, launch, setup_bot

from bcc import execution
from bcc import market_clock as mc
from bcc.config import condition, group, validate
from bcc.execution import ExecutionRejected, PaperExecutor
from bcc.lifecycle import Lifecycle
from bcc.market import DataUnavailable, PublicData
from bcc.storage import AccountingError, Repository, Store, atomic_json, initial_state
from bcc.strategies import Grid, PumpDump
from bcc.trading import exit_position
from bcc.transfer import export_bots, import_bots


@pytest.fixture
def fixed_clock(monkeypatch):
    timer = {"wall": 1000.0, "mono": 100.0}
    monkeypatch.setattr(mc.time, "time", lambda: timer["wall"])
    monkeypatch.setattr(mc.time, "monotonic", lambda: timer["mono"])
    reference = mc.MarketClock()
    reference._reference = (1001.0, 1001.2, 1000.0, 100.0)
    monkeypatch.setattr(mc, "clock", reference)
    monkeypatch.setattr("bcc.market.clock", reference)
    monkeypatch.setattr(reference, "ensure", lambda: None)
    return timer


def test_depth_expiring_during_calculation_never_consumes_liquidity(
    fixed_clock, monkeypatch
):
    snapshot = book()
    snapshot["at"] = 1001.0
    original = deepcopy(snapshot)
    floor = execution.floor_quantity

    def slow_floor(*args):
        result = floor(*args)
        fixed_clock["wall"] += 3
        fixed_clock["mono"] += 3
        return result

    monkeypatch.setattr(execution, "floor_quantity", slow_floor)
    with pytest.raises(ExecutionRejected, match="во время расчёта"):
        PaperExecutor(snapshot).estimate(
            "LONG", "ENTRY", config()["settings"], quantity=1
        )
    assert snapshot == original


def test_quote_age_survives_pc_skew_and_expires_without_retimestamp(fixed_clock):
    q = mc.observed_quote({"bid": 99, "ask": 101, "at": 1001.1})
    assert q["at"] == 1001.1 and mc.quote_is_recent(q)
    fixed_clock["wall"] += 14
    fixed_clock["mono"] += 14
    assert mc.quote_is_recent(q)
    fixed_clock["wall"] += 1
    fixed_clock["mono"] += 1
    assert not mc.quote_is_recent(q)
    assert q["at"] == 1001.1


@pytest.mark.parametrize("jump", [-100, 100])
def test_receipt_proof_rejects_wall_clock_jump_and_future_legacy_quote(
    fixed_clock, jump
):
    q = mc.observed_quote({"at": 1001.1})
    fixed_clock["wall"] += jump
    assert not mc.quote_is_recent(q)
    assert not mc.quote_is_recent({"at": fixed_clock["wall"] + 1})


def test_inverted_quote_uncertainty_is_not_fresh(fixed_clock):
    q = mc.observed_quote({"at": 1001.1})
    q.update(source_age_low=2, source_age_high=1)
    assert not mc.quote_is_recent(q)


def instrument():
    return dict(
        symbol="BTCUSDT",
        status="Trading",
        contractType="LinearPerpetual",
        quoteCoin="USDT",
        lotSizeFilter=dict(
            qtyStep="0.001",
            minOrderQty="0.001",
            minNotionalValue="5",
            maxMktOrderQty="100",
        ),
        priceFilter=dict(tickSize="0.01"),
    )


@pytest.mark.parametrize("source_age", [0.1, 4])
def test_transport_uses_book_timestamp_not_response_or_receipt(
    fixed_clock, monkeypatch, source_age
):
    source = PublicData()
    stamp = 1001.0 - source_age

    def get(url, params=None, ttl=0):
        if "instruments" in url:
            return {"result": {"list": [instrument()]}}
        return dict(
            retCode=0,
            time=1001100,
            result=dict(
                s="BTCUSDT",
                ts=stamp * 1000,
                cts=(stamp - 0.01) * 1000,
                u=13,
                seq=123,
                b=[["99", "2"]],
                a=[["101", "2"]],
            ),
        )

    monkeypatch.setattr(source, "get", get)
    try:
        if source_age > 3:
            with pytest.raises(DataUnavailable, match="возраст=\\["):
                source.orderbook("BTCUSDT")
        else:
            result = source.orderbook("BTCUSDT")
            assert result["at"] == stamp
            assert result["response_at"] == 1001.1
            assert result["engine_at"] == pytest.approx(stamp - 0.01)
            assert result["max_qty"] == 100
    finally:
        source.close()


def test_zero_ttl_and_backward_wall_jump_do_not_reuse_memory_snapshot(
    fixed_clock, monkeypatch
):
    source = PublicData()
    calls = []

    class Session:
        def get(self, *a, **kw):
            calls.append(1)
            return type(
                "Response",
                (),
                dict(
                    status_code=200,
                    headers={},
                    raise_for_status=lambda s: None,
                    json=lambda s: {"count": len(calls)},
                ),
            )()

    source._thread.session = Session()
    url = "https://api.bybit.com/v5/market/orderbook"
    try:
        assert source.get(url)["count"] == 1
        assert source.get(url)["count"] == 2
        fixed_clock["wall"] -= 1
        assert source.get(url, ttl=10)["count"] == 3
        assert source._thread.last_read["via"] == "http"
        assert source.get(url, ttl=10)["count"] == 3
        assert source._thread.last_read["via"] == "memory_cache"
    finally:
        source.close()


def test_candles_reload_full_window_after_long_pause(monkeypatch):
    source = PublicData()
    monkeypatch.setattr("bcc.market.clock.ensure", lambda: None)
    monkeypatch.setattr("bcc.market.market_now", lambda: 12000.0)
    current = bars([100] * 100, 12000000 - 99 * 60000)
    previous = bars([100] * 100, current[0]["t"] - 20 * 60000)
    source.cache[("bars", "BTCUSDT")] = (0, previous)
    limits = []

    def bybit(route, params):
        limits.append(params["limit"])
        rows = [
            [str(b["t"]), "100", "101", "99", "100", "10"]
            for b in reversed(current[-params["limit"] :])
        ]
        return {"result": {"list": rows}}

    monkeypatch.setattr(source, "bybit", bybit)
    try:
        assert len(source.candles("BTCUSDT", 100)) == 100
        assert limits == [100]
    finally:
        source.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("quantity", float("nan")),
        ("quantity", float("inf")),
        ("quantity", -1),
        ("quantity", True),
        ("limit", 0),
    ],
)
def test_invalid_execution_parameters_never_mutate_depth(field, value):
    snapshot = book()
    before = deepcopy(snapshot)
    args = {"quantity": 1, field: value}
    with pytest.raises(ExecutionRejected):
        PaperExecutor(snapshot).estimate("LONG", "ENTRY", config()["settings"], **args)
    assert snapshot == before


def test_exhausted_top_level_uses_next_positive_level_and_market_maximum():
    snapshot = book()
    snapshot["asks"] = [[1, 0], [100, 2]]
    snapshot["max_qty"] = 1
    fill = PaperExecutor(snapshot).estimate(
        "LONG", "ENTRY", config()["settings"], notional=100
    )
    assert fill.quantity <= 1 and fill.price >= 100
    assert -0.5 <= fill.source_age_low <= fill.source_age_high <= 3
    before = deepcopy(snapshot)
    with pytest.raises(ExecutionRejected, match="максимума"):
        PaperExecutor(snapshot).estimate(
            "LONG", "ENTRY", config()["settings"], quantity=2
        )
    assert snapshot == before


def test_seeded_execution_invariants_and_source_immutability(monkeypatch):
    monkeypatch.setattr("bcc.market_clock.utc_bounds", lambda: (1000, 1000.1))
    rng = random.Random(int(os.environ.get("BCC_AUDIT_SEED", "0")))
    settings = config()["settings"]
    for _ in range(100):
        side = rng.choice(["LONG", "SHORT"])
        price = rng.uniform(1, 100000)
        snapshot = book(price, rng.uniform(0.01, 1))
        stamp = 1000 - rng.uniform(-0.49, 4)
        snapshot["at"] = stamp
        budget = rng.uniform(0.1, 100)
        before = deepcopy(snapshot)
        try:
            fill = PaperExecutor(snapshot).estimate(
                side, "ENTRY", settings, notional=budget
            )
        except ExecutionRejected:
            assert snapshot == before
        else:
            assert -0.5 <= fill.source_age_low <= fill.source_age_high <= 3
            assert fill.notional <= budget * (1 + 1e-9)
            assert fill.quantity > 0 and fill.fee >= 0
            assert snapshot["at"] == stamp
            assert all(
                q >= 0
                for levels in (snapshot["asks"], snapshot["bids"])
                for p, q in levels
            )


def test_execution_journal_contains_age_evidence(tmp_path):
    store = Store(tmp_path, 500)
    with store.transaction() as (state, events):
        open_position(state, events, config()["settings"])
        exit_position(state, events, "BTCUSDT:main", 100, "TEST")
        state.pop("_books")
    store.audit()
    assert len(store.history()) == 2
    for e in store.history():
        assert e["book_at"] > 0
        assert -0.5 <= e["book_age_lower"] <= e["book_age_upper"] <= 3


@pytest.mark.parametrize("change", [1, -1])
def test_realized_cannot_be_edited_without_ledger_event(tmp_path, change):
    store = Store(tmp_path, 500)
    with pytest.raises(AccountingError, match="в обход"):
        with store.transaction() as (state, events):
            state["realized"] += change
    assert store.read()["realized"] == 0
    corrupted = store.read()
    corrupted["realized"] = change
    with store.connect() as db:
        db.execute("UPDATE state SET document=?", (json.dumps(corrupted),))
    with pytest.raises(AccountingError, match="Результат"):
        store.audit()


@pytest.mark.parametrize("field,value", [("qty", 0), ("average", 0), ("side", "BAD")])
def test_corrupt_position_rejected_before_restart(field, value):
    state = initial_state(500)
    open_position(state, [], config()["settings"])
    state["positions"]["BTCUSDT:main"][field] = value
    with pytest.raises(AccountingError):
        Store.validate_state(state)


def test_max_length_bot_can_be_copied_and_imported(tmp_path):
    repo = Repository(tmp_path / "app")
    cfg = config()
    cfg["name"] = "Я" * 80
    original = repo.create(cfg)
    copied = repo.copy(original)
    assert len(repo.load(copied)[0]["name"]) == 80
    assert repo.load(copied)[0]["name"].endswith(" — копия")
    archive = tmp_path / "bots.zip"
    export_bots(repo, [original], archive)
    imported = import_bots(repo, archive)
    assert len(repo.load(imported[0])[0]["name"]) == 80
    assert repo.load(imported[0])[0]["name"].endswith(" — импорт")


@pytest.mark.parametrize("fault", ["schema", "reference_tf", "label"])
def test_bool_schema_timeframe_and_invalid_section_label_are_rejected(fault):
    cfg = config()
    if fault == "schema":
        cfg["schema"] = True
    elif fault == "label":
        cfg["sections"][0]["label"] = ""
    else:
        c = condition()
        c.update(reference="indicator", reference_period=14, reference_timeframe=True)
        cfg["sections"][0]["entry"] = group(c)
    with pytest.raises(ValueError):
        validate(cfg)


@pytest.mark.parametrize("direction", [1, -1])
def test_pump_spread_without_price_reversal_cannot_trigger_entry(direction):
    cfg = config("PUMP_DUMP")
    state = initial_state(500)
    now = int(time.time() * 1000)
    values = [100] * 27 + [
        100 + 10 * direction,
        100 + 15 * direction,
        100 + 20 * direction,
    ]
    history = bars(values, now // 60000 * 60000 - 30 * 60000)
    for b in history[-3:]:
        b["volume"] = 1000
    attach(state, price=values[-1])
    events = []
    PumpDump().process(
        cfg, state, events, "BTCUSDT", history, values[-1] - 2, values[-1] + 2, now
    )
    assert state["strategy"]["BTCUSDT"].get("impulse")
    assert not state["positions"] and not events


def test_grid_limit_prevents_consumption_beyond_level():
    cfg = config("GRID")
    cfg["grid"]["levels"] = 1
    cfg["settings"]["execution_policy"] = "fok"
    state = initial_state(500)
    snapshot = book()
    snapshot["asks"] = [[100, 0.01], [102, 100]]
    state["_books"] = {"BTCUSDT": snapshot}
    events = []
    Grid().process(cfg, state, events, "BTCUSDT", [], 99, 100, int(time.time() * 1000))
    assert not state["positions"] and not events
    assert snapshot["asks"][0][1] == 0.01


def test_delayed_old_stop_cannot_stop_a_new_worker(tmp_path):
    repo = Repository(tmp_path / "app")
    life = Lifecycle(repo)
    runner = tmp_path / "worker.py"
    runner.write_text(RUNNER, encoding="utf-8")
    folder, _ = setup_bot(repo, runner)
    proc = launch(life, folder, runner)
    try:
        atomic_json(folder / "stop.request", {"id": "delayed", "token": "previous-run"})
        time.sleep(0.4)
        assert proc.poll() is None and life.status(folder) == "Работает"
        life.stop(folder)
        assert (
            json.loads((folder / "stop.request").read_text(encoding="utf-8"))["token"]
            == life.tokens[str(folder)]
        )
        proc.wait(timeout=7)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            life.stop(folder, force=True)
            proc.wait(timeout=7)


def test_report_distinguishes_old_missing_proof_from_invalid_and_fresh_fills():
    from bcc.support import execution_freshness

    legacy = dict(id="old", action="OPEN")
    valid = dict(
        id="fresh", action="CLOSE", book_at=1000, book_age_lower=0.2, book_age_upper=0.5
    )
    assert execution_freshness([])["status"] == "NO_FILLS"
    assert execution_freshness([valid])["status"] == "PASS"
    assert execution_freshness([legacy, valid])["status"] == "PARTIAL_EVIDENCE"
    invalid = dict(valid, id="stale", book_age_upper=3.001)
    report = execution_freshness([valid, invalid, legacy])
    assert report["status"] == "FAIL" and report["invalid_event_ids"] == ["stale"]
    assert report["with_age_evidence"] == 2 and report["without_age_evidence"] == 1


def test_health_cannot_claim_pass_when_depth_ages_after_transport(monkeypatch):
    from bcc import health

    closed = []

    class Source:
        def candles(self, *args):
            return [dict(t=1000000)]

        def orderbook(self, *args):
            return book()

        def close(self):
            closed.append(True)

    monkeypatch.setattr(health, "PublicData", Source)
    monkeypatch.setattr(health, "book_age_bounds", lambda stamp: (2.9, 3.001))
    with pytest.raises(DataUnavailable, match="завершения проверки"):
        health.check_bybit()
    assert closed == [True]


@pytest.mark.parametrize("target_name", ["paper.sqlite", "backup.zip"])
def test_transfer_cannot_overwrite_a_file_inside_my_bots(tmp_path, target_name):
    repo = Repository(tmp_path / "app")
    folder = repo.create(config())
    Store(folder, 500)
    target = folder / target_name
    if target_name.endswith(".zip"):
        target.write_bytes(b"existing archive")
    before = target.read_bytes()
    with pytest.raises(ValueError, match="вне папки MY_BOTS"):
        export_bots(repo, [folder], target)
    assert target.read_bytes() == before
    Store(folder).audit()
