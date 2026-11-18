"""System-level VELES per-coin capital allocation across sections.

All scenarios use a NEW synthetic PAPER state; no exchange/network orders.
The existing independent shared risk and mark-tier checks are not relaxed.
"""
from copy import deepcopy
import random

import pytest

from bcc.coin_budget import (
    admission_reason, committed, limit_amount, validate_capital_limit,
)
from bcc.config import defaults, validate, group, condition
from bcc.constructor_contract import (
    project_veles_coin_transaction, validate_constructor_roundtrip,
)
from bcc.storage import Repository, initial_state
from bcc.trading import enter
from bcc.strategies import Veles
from test_core import attach, bars


def setup_config():
    config = defaults("VELES")
    config["name"] = "VELES capital budget test"
    config["symbols"] = ["BTCUSDT", "ETHUSDT"]
    config["settings"]["orders"]["count"] = 1
    config["settings"]["allocation_pct"] = 5.0
    for section in config["sections"]:
        section["entry"] = group(condition("PRICE", 0, ">", 1))
        section["exit"] = group(condition("PRICE", 1e9, ">", 1))
    return config


@pytest.mark.parametrize("mode,value,expected", [
    ("percent", 5.0, 25.0),
    ("percent", 100.0, 500.0),
    ("fixed", 20.0, 20.0),
    ("fixed", 1000.0, 1000.0),
])
def test_coin_limit_resolves_exactly_by_mode(mode, value, expected):
    config = setup_config()
    config["coin_overrides"]["BTCUSDT"] = {
        "capital_limit": {"mode": mode, "value": value},
    }
    assert validate(config) == config
    assert limit_amount(config, "BTCUSDT") == pytest.approx(expected)
    assert limit_amount(config, "ETHUSDT") is None


def test_legacy_configuration_is_unlimited_and_unchanged():
    config = setup_config()
    before = deepcopy(config)
    assert limit_amount(config, "BTCUSDT") is None
    assert validate(config) == before
    assert config == before
    state = initial_state(500)
    assert not admission_reason(state, "BTCUSDT", 99999, None)


@pytest.mark.parametrize("bad", [
    {"mode": "percent", "value": 0},
    {"mode": "percent", "value": 100.001},
    {"mode": "fixed", "value": 0},
    {"mode": "fixed", "value": -1},
    {"mode": "fixed", "value": float("nan")},
    {"mode": "fixed", "value": float("inf")},
    {"mode": "fixed", "value": True},
    {"mode": "percent", "value": "5"},
    {"mode": "weight", "value": 25},
    {"mode": "fixed", "value": 20, "ignored": 1},
    {"mode": "fixed"},
    {},
    None,
])
def test_invalid_capital_rules_rejected_in_single_config_contract(bad):
    config = setup_config()
    config["coin_overrides"]["BTCUSDT"] = {"capital_limit": bad}
    with pytest.raises((ValueError, TypeError)):
        validate(config)


def test_other_strategies_cannot_silently_ignore_a_coin_limit():
    config = defaults("GRID")
    config["name"] = "Invalid unrelated limit"
    config["coin_overrides"]["BTCUSDT"] = {
        "capital_limit": {"mode": "fixed", "value": 25}
    }
    with pytest.raises(ValueError, match="VELES"):
        validate(config)


def test_nominal_cap_counts_filled_remaining_dca_and_pending_once():
    state = initial_state(500)
    state["positions"] = {
        "btc_first": {"symbol": "BTCUSDT", "margin": 10, "order_margin": 15},
        "btc_second": {"symbol": "BTCUSDT", "margin": 4, "order_margin": 6},
        "eth": {"symbol": "ETHUSDT", "margin": 90, "order_margin": 10},
    }
    state["pending"] = {
        "btc_next": {"symbol": "BTCUSDT", "margin": 5},
        "eth_next": {"symbol": "ETHUSDT", "margin": 20},
    }
    assert committed(state, "BTCUSDT") == 40
    assert committed(state, "ETHUSDT") == 120
    assert not admission_reason(state, "BTCUSDT", 10, 50)
    assert "предел 50" in admission_reason(state, "BTCUSDT", 11, 50)
    assert not admission_reason(state, "ETHUSDT", 10, None)


def test_corrupt_persisted_coin_reserve_fails_closed():
    state = initial_state(500)
    state["positions"]["bad"] = {
        "symbol": "BTCUSDT", "margin": 1, "order_margin": float("nan"),
    }
    assert "Недостоверное" in admission_reason(state, "BTCUSDT", 5, 50)
    state["positions"]["bad"]["order_margin"] = -1
    assert "Недостоверное" in admission_reason(state, "BTCUSDT", 5, 50)


def test_low_level_trade_entry_respects_coin_limit_before_books_or_events():
    config = setup_config()
    state = initial_state(500)
    state["positions"]["already"] = {
        "symbol": "BTCUSDT", "margin": 18.0, "order_margin": 7.0,
    }
    events = []
    result, why = enter(
        state, events, "BTCUSDT:new", "BTCUSDT", "LONG",
        config["settings"], 100, 25.0,
        [{"price": 100, "weight": 1}], "new",
        coin_cap=25.0,
    )
    assert result is False
    assert "Лимит капитала BTCUSDT" in why
    assert len(state["positions"]) == 1 and not events


def test_pending_conversion_frees_its_own_nominal_hold_before_reentry():
    state = initial_state(500)
    state["pending"]["one"] = {"symbol": "BTCUSDT", "margin": 25}
    assert admission_reason(state, "BTCUSDT", 25, 25)
    pending = state["pending"].pop("one")
    assert not admission_reason(state, "BTCUSDT", pending["margin"], 25)
    state["positions"]["filled"] = {
        "symbol": "BTCUSDT", "margin": 5, "order_margin": 20,
    }
    assert committed(state, "BTCUSDT") == 25


def test_two_veles_sections_do_not_double_spend_single_coin_budget():
    cfg = setup_config()
    cfg["coin_overrides"]["BTCUSDT"] = {
        "capital_limit": {"mode": "percent", "value": 5.0},
    }
    state, events = initial_state(500), []
    attach(state)
    Veles().process(
        cfg, state, events, "BTCUSDT",
        bars([100, 100]), 99.99, 100.01, 120000,
    )
    assert len(state["positions"]) == 1
    assert sum(e["action"] == "OPEN" for e in events) == 1
    assert any(
        "Лимит капитала BTCUSDT" in row.get("reason", "")
        for row in state["diagnostics"]["BTCUSDT"]
    )
    assert committed(state, "BTCUSDT") <= 25 + 1e-6


def test_smaller_coin_cap_reports_exact_blocker_not_silent_zero_position():
    cfg = setup_config()
    cfg["coin_overrides"]["BTCUSDT"] = {
        "capital_limit": {"mode": "fixed", "value": 20.0},
    }
    state, events = initial_state(500), []
    attach(state)
    Veles().process(
        cfg, state, events, "BTCUSDT",
        bars([100, 100]), 99.99, 100.01, 120000,
    )
    assert not state["positions"]
    assert not events
    assert any(
        "новая секция 25 USDT" in row.get("reason", "")
        and "предел 20 USDT" in row.get("reason", "")
        for row in state["diagnostics"]["BTCUSDT"]
    )


def test_new_cap_only_blocks_future_allocations_not_open_positions():
    state = initial_state(500)
    state["positions"] = {
        "old": {"symbol": "BTCUSDT", "margin": 100, "order_margin": 25},
    }
    before = deepcopy(state)
    assert "Лимит капитала BTCUSDT" in admission_reason(state, "BTCUSDT", 5, 80)
    assert state == before
    assert state["positions"]["old"]["margin"] == 100


def test_no_hardcoded_19_or_20_global_position_limit():
    from bcc.shared_risk import DEFAULT_POLICY, validate_policy
    assert DEFAULT_POLICY["max_positions"] == 0
    assert validate_policy({"max_positions": 50, "max_committed_usdt": 0})["max_positions"] == 50
    assert validate_policy({"max_positions": 200, "max_committed_usdt": 0})["max_positions"] == 200


def test_coin_budget_edit_is_atomic_and_survives_repository_roundtrip(tmp_path):
    cfg = setup_config()
    updated = project_veles_coin_transaction(cfg, coin_rules={
        "BTCUSDT": {"capital_limit": {"mode": "fixed", "value": 80.0}},
        "ETHUSDT": {"capital_limit": {"mode": "percent", "value": 12.5}},
    })
    assert cfg["coin_overrides"] == {}
    assert validate_constructor_roundtrip(updated) == updated
    assert limit_amount(updated, "BTCUSDT") == 80.0
    assert limit_amount(updated, "ETHUSDT") == 62.5
    repo = Repository(tmp_path)
    folder = repo.create(updated)
    loaded, revision = repo.load(folder)
    assert loaded == updated
    reset = project_veles_coin_transaction(
        loaded, coin_rules={"BTCUSDT": None})
    repo.save(folder, reset, revision)
    result, _ = repo.load(folder)
    assert limit_amount(result, "BTCUSDT") is None
    assert limit_amount(result, "ETHUSDT") == 62.5


def test_invalid_bulk_edit_rolls_back_every_coin():
    cfg = setup_config()
    before = deepcopy(cfg)
    with pytest.raises(ValueError):
        project_veles_coin_transaction(cfg, coin_rules={
            "BTCUSDT": {"capital_limit": {"mode": "fixed", "value": 25.0}},
            "ETHUSDT": {"capital_limit": {"mode": "percent", "value": 150}},
        })
    assert cfg == before


@pytest.mark.parametrize("seed", range(10))
def test_randomized_two_coin_budget_independence(seed):
    rng = random.Random(70000 + seed)
    cfg = setup_config()
    btc = rng.randint(1, 95)
    eth = rng.randint(1, 100)
    changed = project_veles_coin_transaction(cfg, coin_rules={
        "BTCUSDT": {"capital_limit": {"mode": "percent", "value": btc}},
        "ETHUSDT": {"capital_limit": {"mode": "fixed", "value": eth}},
    })
    assert limit_amount(changed, "BTCUSDT") == pytest.approx(500 * btc / 100)
    assert limit_amount(changed, "ETHUSDT") == eth
    assert cfg["coin_overrides"] == {}
