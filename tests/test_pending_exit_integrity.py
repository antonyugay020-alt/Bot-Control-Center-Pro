"""Outstanding exit intent is immutable until the serialized retry completes."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from bcc.config import defaults, condition, group
from bcc.storage import initial_state
from bcc.strategies import Veles
from bcc.trading import enter, plan, protective_exit
from test_core import attach, bars


def opened(section_id=None):
    cfg = defaults("VELES")
    cfg["symbols"] = ["BTCUSDT"]
    cfg["sections"] = cfg["sections"][:1]
    cfg["settings"].update(leverage=1, allocation_pct=10, slippage_pct=0)
    cfg["settings"]["orders"].update(count=2, coverage_pct=20)
    cfg["settings"]["profit"].update(mode="simple", pct=1)
    cfg["settings"]["stop"].update(mode="both", pct=2, signal=group(condition("PRICE", 0, ">", 1)))
    state, events = initial_state(500), []
    attach(state)
    section = cfg["sections"][0]
    if section_id is not None:
        section['id'] = section_id
    key = "BTCUSDT:" + section["id"]
    success, reason = enter(state, events, key, "BTCUSDT", "LONG", cfg["settings"],
                            100, 50, plan(cfg["settings"]["orders"], "LONG", 100), section["id"])
    assert success, reason
    state["positions"][key]["exit_signal"] = deepcopy(section["exit"])
    return cfg, state, events, key


@pytest.mark.parametrize("reason", ["MANUAL_PAPER_CLOSE", "SIGNAL_EXIT", "MULTI_TP", "PAPER_LIQUIDATION"])
def test_protective_exit_does_not_replace_saved_exit(reason):
    cfg, state, events, key = opened()
    pending = {"reason": reason, "ratio": 0.35, "limit": 101.0, "target_index": 0}
    state["positions"][key]["exit_pending"] = deepcopy(pending)
    prior_qty = state["positions"][key]["qty"]
    attach(state, price=110)
    assert protective_exit(state, events, key, 109.99, 110.01) is False
    assert state["positions"][key]["exit_pending"] == pending
    assert state["positions"][key]["qty"] == prior_qty
    assert [e["action"] for e in events] == ["OPEN"]


def test_veles_stop_and_dca_do_not_override_pending_manual_exit(monkeypatch):
    cfg, state, events, key = opened()
    pending = {"reason": "MANUAL_PAPER_CLOSE", "ratio": 1.0,
               "limit": None, "target_index": None}
    state["positions"][key]["exit_pending"] = deepcopy(pending)
    before = deepcopy(state["positions"][key])
    calls = []

    def fake_signal(*args):
        calls.append(args)
        return SimpleNamespace(matched=True, available=True, observations=[])

    monkeypatch.setattr("bcc.strategies.signal", fake_signal)
    attach(state, price=75)
    # Its own entry condition must not fire during the test; the position is
    # already open and a saved close must not create another trading action.
    cfg["sections"][0]["enabled"] = False
    Veles().process(cfg, state, events, "BTCUSDT", bars([100, 75]),
                    74.99, 75.01, 120000)
    assert state["positions"][key] == before
    assert state["positions"][key]["exit_pending"] == pending
    assert [e["action"] for e in events] == ["OPEN"]
    assert not calls
    assert "Ожидание исполнения" in state["diagnostics"]["BTCUSDT"][0]["reason"]

@pytest.mark.parametrize("strategy", ["GRID", "PUMP_DUMP"])
def test_other_engines_preserve_pending_manual_close(strategy):
    from bcc.strategies import Grid, PumpDump

    cfg, state, events, key = opened(section_id='fast' if strategy == 'PUMP_DUMP' else None)
    p = state["positions"][key]
    p["exit_pending"] = {"reason": "MANUAL_PAPER_CLOSE", "ratio": 1.0,
                         "limit": None, "target_index": None}
    if strategy == "GRID":
        p["strategy_params"] = {"take_profit_pct": 0.1, "stop_loss_pct": 0.1}
        engine = Grid()
        cfg["grid"]["levels"] = 1
    else:
        p["strategy_params"] = cfg["pump"]["fast"]
        engine = PumpDump()
    before = deepcopy(p)
    attach(state, price=80)
    # process_tick blocks new admissions while an outstanding manual close
    # exists; preserve that real integration contract in this unit fixture.
    state["_entry_blocked"] = "Выполняется ручное закрытие PAPER-позиций"
    engine.process(cfg, state, events, "BTCUSDT", bars([100] * 3),
                   79.99, 80.01, 120000)
    assert state["positions"][key] == before
    assert [e["action"] for e in events] == ["OPEN"]
    runtime = state["strategy"].get("BTCUSDT", {})
    assert not runtime.get("last_exit")
    assert not runtime.get("cooldowns")

@pytest.mark.parametrize("strategy", ["GRID", "PUMP_DUMP"])
def test_deferred_close_sets_cooldown_only_after_full_fill(strategy, tmp_path):
    from bcc.runtime import process_tick
    from bcc.scheduler import MarketTick
    from bcc.storage import Store
    from bcc.trading import exit_position
    from test_core import book
    from bcc.strategies import Grid, PumpDump

    cfg, state, events, original = opened(section_id='fast' if strategy == 'PUMP_DUMP' else None)
    cfg["strategy"] = strategy
    cfg["settings"]["orders"]["count"] = 1
    # Persist a low-notional trade with a standard PaperExecutor book.
    if strategy == "GRID":
        state["positions"][original]["strategy_params"] = {
            "take_profit_pct": 10, "stop_loss_pct": 10,
        }
        cfg["grid"]["levels"] = 1
        engine = Grid()
    else:
        state["positions"][original]["strategy_params"] = cfg["pump"]["fast"]
        engine = PumpDump()
    p = state["positions"][original]
    state["_books"] = {"BTCUSDT": book(100, size=0.05)}
    assert exit_position(state, events, original, 100, "SIGNAL_EXIT")
    assert original in state["positions"]
    assert p["exit_pending"]["reason"] == "SIGNAL_EXIT"
    assert not state["strategy"].get("BTCUSDT", {}).get("last_exit")
    assert not state["strategy"].get("BTCUSDT", {}).get("cooldowns")
    state.pop("_books")
    store = Store(tmp_path, 500)
    with store.transaction() as (saved, history):
        saved.update(state)
        history.extend(events)
    # The retry is completed at the next tick, after process restart.
    # No new admissions during the tick: simulate a risk-manager block.
    class NoNewEntries:
        def process(self, cfg, state, events, symbol, bars, bid, ask, now_ms):
            pass
    process_tick(cfg, store, NoNewEntries(),
                 MarketTick("BTCUSDT", [], book(100, size=100), [], {}))
    restored = store.read()
    assert original not in restored["positions"]
    runtime = restored["strategy"]["BTCUSDT"]
    if strategy == "GRID":
        assert runtime["last_exit"][original] > 0
    else:
        assert runtime["cooldowns"]["fast"] > 0
    store.audit()
