"""Low-level PAPER entry MUST enforce the same coin quota as VELES.

These tests call the real entry routine but intentionally omit market books,
so rejected entries cannot create a fill or change a balance. No exchange,
network, worker or persistent account is involved.
"""
from copy import deepcopy

import pytest

from bcc.config import defaults, validate
from bcc.storage import initial_state
from bcc.trading import enter


def inputs():
    cfg = defaults("VELES")
    cfg["name"] = "low level budget safety"
    cfg["settings"].update(
        allocation_mode="coin_fixed", allocation_fixed_usdt=25.0,
        leverage=2.0, fee_rate=0.0006,
    )
    validated = validate(cfg)
    state = initial_state(500)
    events = []
    settings = deepcopy(validated["settings"])
    orders = [{"price": 100.0, "weight": 1.0}]
    return state, events, settings, orders


def attempt(state, events, settings, orders, **kwargs):
    return enter(
        state, events, "BTCUSDT:section-a", "BTCUSDT", "LONG",
        settings, 100.0, 20.0, orders, "section-a", **kwargs,
    )


def test_direct_coin_entry_requires_trusted_budget_from_strategy():
    state, events, settings, orders = inputs()
    before = deepcopy(state)
    success, reason = attempt(state, events, settings, orders)
    assert not success
    assert "Не подтверждена общая квота" in reason
    assert state == before
    assert events == []


def test_executor_blocks_oversubscribed_coin_before_any_mutation():
    state, events, settings, orders = inputs()
    state["pending"]["other-section"] = {
        "symbol": "BTCUSDT", "margin": 10.0,
    }
    before = deepcopy(state)
    success, reason = attempt(
        state, events, settings, orders, coin_budget=25.0,
    )
    assert not success
    assert "Общая квота монеты BTCUSDT" in reason
    assert "15 USDT" in reason
    assert state == before
    assert events == []


@pytest.mark.parametrize("budget", [None, -1, 0, True, float("nan"), 25j])
def test_untrusted_budget_never_enters_position(budget):
    state, events, settings, orders = inputs()
    success, reason = attempt(
        state, events, settings, orders, coin_budget=budget)
    assert not success
    assert "квота" in reason.lower()
    assert not events
    assert not state["positions"]


def test_existing_pending_grid_is_not_charged_for_its_reservation_twice():
    state, events, settings, orders = inputs()
    # A pending grid was already counted when the reservation was created.
    # Veles removes it from state.pending for the atomic fill transition.
    # At that point the low-level executor may proceed to the normal
    # public-book check even without a fresh coin-budget admission.
    success, reason = attempt(
        state, events, settings, orders, existing_grid=True)
    assert not success
    assert "стакана" in reason
    assert "квота" not in reason.lower()
    assert not events


def test_legacy_mode_keeps_old_entry_path_without_coin_budget():
    state, events, settings, orders = inputs()
    settings["allocation_mode"] = "section_pct"
    success, reason = attempt(state, events, settings, orders)
    assert not success
    assert "стакана" in reason
    assert "квота" not in reason.lower()
    assert not events


def test_non_veles_sizing_does_not_accept_hidden_coin_mode():
    for strategy in ("GRID", "PUMP_DUMP", "POLYMARKET"):
        cfg = defaults(strategy)
        cfg["name"] = "wrong allocation"
        cfg["settings"]["allocation_mode"] = "coin_fixed"
        with pytest.raises(ValueError, match="Квота маржи"):
            validate(cfg)
