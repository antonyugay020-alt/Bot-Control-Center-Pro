"""Real VELES signal -> PAPER executor integration for per-coin capital.

Market books are synthetic in-memory fixtures, not real Bybit/Polymarket.
No live bots, persisted trading accounts or external processes.
"""
from copy import deepcopy

from test_core import attach, bars
from test_veles_behaviour import single_section

from bcc.config import validate
from bcc.storage import initial_state
from bcc.strategies import Veles


def two_runnable_sections(mode, *, coin_budget=50):
    c = single_section()
    c["name"] = "two independent ranges"
    sec = deepcopy(c["sections"][0])
    sec["id"] = "second-range"
    sec["label"] = "Second range"
    c["sections"].append(sec)
    c["settings"].update(
        allocation_mode=mode, allocation_fixed_usdt=coin_budget,
        allocation_pct=10,
    )
    return validate(c)


def run_both(cfg):
    state, events = initial_state(500), []
    attach(state)
    Veles().process(
        cfg, state, events, "BTCUSDT",
        bars([100, 100]), 99.99, 100.01, 120000,
    )
    return state, events


def test_fixed_coin_budget_allows_only_one_full_grid_per_same_coin():
    cfg = two_runnable_sections("coin_fixed", coin_budget=50)
    state, events = run_both(cfg)
    opens = [e for e in events if e["action"] == "OPEN"]
    assert len(opens) == 1, (state["diagnostics"], events)
    assert len(state["positions"]) == 1
    p = next(iter(state["positions"].values()))
    assert p["budget"] / p["settings"]["leverage"] == 50
    text = " ".join(row["reason"] for row in state["diagnostics"]["BTCUSDT"])
    assert "Квота монеты" in text


def test_percent_coin_budget_also_applies_across_both_sections():
    cfg = two_runnable_sections("coin_pct")
    state, events = run_both(cfg)
    assert sum(e["action"] == "OPEN" for e in events) == 1
    assert len(state["positions"]) == 1


def test_legacy_percentage_retains_two_independent_section_positions():
    cfg = two_runnable_sections("section_pct")
    state, events = run_both(cfg)
    opens = [e for e in events if e["action"] == "OPEN"]
    assert len(opens) == 2, (state["diagnostics"], events)
    assert len(state["positions"]) == 2


def test_per_coin_budget_exhausted_by_unfilled_other_range_reservation():
    cfg = two_runnable_sections("coin_fixed", coin_budget=50)
    state = initial_state(500)
    events = []
    attach(state)
    # A restored pending range already owns this coin's entire principal
    # allocation. It is a different ID than the active constructor rows.
    state["pending"]["BTCUSDT:old-other-section"] = {
        "symbol": "BTCUSDT", "margin": 50,
    }
    Veles().process(
        cfg, state, events, "BTCUSDT",
        bars([100, 100]), 99.99, 100.01, 120000,
    )
    assert not any(e["action"] == "OPEN" for e in events)
    assert not state["positions"]
    assert state["pending"]["BTCUSDT:old-other-section"]["margin"] == 50
