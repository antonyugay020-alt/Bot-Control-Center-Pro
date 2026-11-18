"""VELES percent/fixed-USDT margin — end-to-end configuration and PAPER contract.

These tests use only synthetic configs and state, not real exchange orders,
private bots or running Windows processes.
"""
from copy import deepcopy
import pytest

from bcc.config import (
    defaults, validate, normalize_config, effective, section_margin_budget,
)
from bcc.strategies import capital_margin
from bcc.wizard_presentation import grid_preview_data, summary_rows
from bcc.storage import initial_state
from bcc.trading import enter, exit_position, plan, reserved
from test_core import attach


def bot():
    cfg = defaults("VELES")
    cfg["name"] = "Synthetic VELES capital check"
    cfg["symbols"] = ["BTCUSDT", "ETHUSDT"]
    return cfg


def test_legacy_percent_policy_is_preserved_without_edit():
    cfg = bot()
    del cfg["settings"]["allocation_mode"]
    del cfg["settings"]["allocation_usdt"]
    original = deepcopy(cfg)
    upgraded = normalize_config(cfg)
    assert cfg == original
    assert upgraded["settings"]["allocation_mode"] == "percent"
    assert upgraded["settings"]["allocation_usdt"] == 25.0
    assert validate(cfg)["settings"]["allocation_mode"] == "percent"
    state = initial_state(cfg["capital"])
    assert capital_margin(upgraded, state, effective(upgraded, "BTCUSDT")) == 25


def test_fixed_usdt_is_margin_not_leveraged_notional():
    cfg = bot()
    cfg["settings"].update(allocation_mode="fixed_usdt",
                           allocation_usdt=35.5, leverage=4)
    validate(cfg)
    st = initial_state(cfg["capital"])
    margin = capital_margin(cfg, st, effective(cfg, "BTCUSDT"))
    assert margin == pytest.approx(35.5)
    view = grid_preview_data(cfg, "BTCUSDT", cfg["sections"][0]["id"])
    assert "35.5 USDT" in view["status"]
    assert "142 USDT" in view["status"]
    assert "фиксированные USDT" in view["status"]


def test_per_coin_fixed_and_percentage_resolve_independently():
    cfg = bot()
    cfg["coin_overrides"] = {
        "BTCUSDT": {"settings": {"allocation_mode": "fixed_usdt",
                                  "allocation_usdt": 40}},
        "ETHUSDT": {"settings": {"allocation_mode": "percent",
                                  "allocation_pct": 2}},
    }
    validate(cfg)
    state = initial_state(cfg["capital"])
    assert capital_margin(cfg, state, effective(cfg, "BTCUSDT")) == 40
    assert capital_margin(cfg, state, effective(cfg, "ETHUSDT")) == 10


def test_per_section_coin_override_takes_precedence_without_leak():
    cfg = bot()
    sid = cfg["sections"][0]["id"]
    cfg["settings"].update(allocation_mode="fixed_usdt", allocation_usdt=20)
    cfg["coin_overrides"] = {
        "BTCUSDT": {
            "settings": {"allocation_usdt": 30},
            "sections": {sid: {"settings": {"allocation_usdt": 45}}},
        }
    }
    validate(cfg)
    first, second = cfg["sections"]
    st = initial_state(cfg["capital"])
    assert capital_margin(cfg, st, effective(cfg, "BTCUSDT", first)) == 45
    assert capital_margin(cfg, st, effective(cfg, "BTCUSDT", second)) == 30
    assert capital_margin(cfg, st, effective(cfg, "ETHUSDT", first)) == 20


@pytest.mark.parametrize("invalid", [
    {"allocation_mode": "automatic"}, {"allocation_mode": None},
    {"allocation_usdt": 0}, {"allocation_usdt": -1},
    {"allocation_usdt": float("nan")}, {"allocation_usdt": float("inf")},
    {"allocation_usdt": True}, {"allocation_usdt": 1_000_000_001},
])
def test_invalid_mode_or_fixed_amount_fails_before_paper_entry(invalid):
    cfg = bot()
    cfg["settings"].update(invalid)
    with pytest.raises(ValueError):
        validate(cfg)


def test_invalid_coin_override_rejected_before_paper_entry():
    cfg = bot()
    cfg["coin_overrides"] = {
        "BTCUSDT": {"settings": {"allocation_mode": "fixed_usdt",
                                  "allocation_usdt": -12}}
    }
    with pytest.raises(ValueError):
        validate(cfg)


def test_editing_fixed_amount_does_not_modify_unrelated_coins():
    cfg = bot()
    cfg["coin_overrides"] = {
        "BTCUSDT": {"settings": {"allocation_mode": "fixed_usdt",
                                  "allocation_usdt": 50}}
    }
    before = deepcopy(cfg)
    updated = deepcopy(cfg)
    updated["coin_overrides"]["BTCUSDT"]["settings"]["allocation_usdt"] = 65
    assert cfg == before
    assert effective(updated, "ETHUSDT") == effective(cfg, "ETHUSDT")
    assert effective(updated, "BTCUSDT")["allocation_usdt"] == 65


def test_percent_preview_legacy_math_remains_consistent():
    cfg = bot()
    original = deepcopy(cfg)
    view = grid_preview_data(cfg, "BTCUSDT", cfg["sections"][0]["id"])
    assert "25 USDT" in view["status"]
    assert "процент депозита" in view["status"]
    assert cfg == original


def test_wizard_summary_shows_active_global_margin_mode():
    cfg = bot()
    rows = dict(summary_rows(cfg))
    assert rows["Размер секции"] == "5% депозита"
    cfg["settings"]["allocation_mode"] = "fixed_usdt"
    cfg["settings"]["allocation_usdt"] = 33
    rows = dict(summary_rows(cfg))
    assert rows["Размер секции"] == "33 USDT фиксированная маржа"


def test_fixed_coin_margin_real_paper_entry_and_exit_conserve_cash():
    """Exercise the actual PAPER order lifecycle, not just sizing algebra."""
    cfg = bot()
    cfg["coin_overrides"] = {
        "BTCUSDT": {"settings": {"allocation_mode": "fixed_usdt",
                                  "allocation_usdt": 37}}
    }
    validate(cfg)
    state, events = initial_state(500), []
    symbol, key = "BTCUSDT", "BTCUSDT:synthetic_fixed"
    settings = effective(cfg, symbol)
    margin = capital_margin(cfg, state, settings)
    assert margin == 37
    attach(state, symbol=symbol, price=100)
    ok, reason = enter(
        state, events, key, symbol, "LONG", settings, 100, margin,
        plan(settings["orders"], "LONG", 100), "synthetic_fixed",
    )
    assert ok, reason
    assert reserved(state) == pytest.approx(37, abs=0.01)
    attach(state, symbol=symbol, price=110)
    assert exit_position(state, events, key, 110, "SYNTHETIC_EXIT")
    assert state["positions"] == {}
    assert reserved(state) == 0
    assert state["cash"] == pytest.approx(
        cfg["capital"] + sum(event["cash_delta"] for event in events)
    )


def test_fixed_margin_larger_than_free_capital_cannot_force_entry():
    """Fixed allocation does not bypass shared PAPER cash admission."""
    cfg = bot()
    cfg["settings"].update(allocation_mode="fixed_usdt", allocation_usdt=800)
    validate(cfg)
    state, events = initial_state(500), []
    settings = cfg["settings"]
    attach(state, price=100)
    ok, reason = enter(
        state, events, "BTCUSDT:oversized", "BTCUSDT", "LONG",
        settings, 100, capital_margin(cfg, state, settings),
        plan(settings["orders"], "LONG", 100), "oversized",
    )
    assert not ok
    assert reason
    assert not state["positions"]
    assert state["cash"] == 500
