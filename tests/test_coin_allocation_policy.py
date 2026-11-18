"""Full PAPER coin-capital policy contract: schema -> effective inheritance ->
transactional sizing -> constructor preview. All fixtures are in memory.
"""
from copy import deepcopy
import math
import random

import pytest

from bcc.coin_allocation import (
    AllocationUnavailable, SECTION_PCT, COIN_PCT, COIN_FIXED,
    allocation_signature, available_margin_for_coin, occupied_coin_margin,
    planned_margin,
)
from bcc.config import (
    defaults, effective, normalize_config, validate,
)
from bcc.constructor_contract import validate_constructor_roundtrip
from bcc.constructor_review import (
    constructor_change_summary, effective_risk_review_text,
)
from bcc.storage import initial_state
from bcc.strategies import capital_margin
from bcc.wizard_presentation import grid_preview_data, summary_rows


def cfg():
    c = defaults("VELES")
    c["name"] = "coin-budget-fixture"
    return c


def section_policy(mode, *, pct=10.0, fixed=35.0):
    s = deepcopy(defaults("VELES")["settings"])
    s.update(allocation_mode=mode, allocation_pct=pct,
             allocation_fixed_usdt=fixed)
    return s


def old_config():
    c = cfg()
    c["settings"].pop("allocation_mode")
    c["settings"].pop("allocation_fixed_usdt")
    return c


def open_position(symbol, *, margin=5.0, order_margin=0.0,
                  budget=None, leverage=2.0):
    p = {"symbol": symbol, "kind": "VELES", "margin": margin,
         "order_margin": order_margin, "settings": {"leverage": leverage}}
    if budget is not None:
        p["budget"] = budget
    return p


def test_legacy_config_is_upgraded_without_changing_effective_percentage():
    original = old_config()
    normalized = normalize_config(original)
    assert original["settings"].get("allocation_mode") is None  # pure
    assert normalized["settings"]["allocation_mode"] == SECTION_PCT
    assert normalized["settings"]["allocation_fixed_usdt"] == 25.0
    stored = validate(original)
    assert stored["settings"]["allocation_mode"] == SECTION_PCT
    assert capital_margin(stored, initial_state(500), stored["settings"],
                          symbol="BTCUSDT") == pytest.approx(25.0)


def test_legacy_section_percentage_does_not_start_capping_open_ranges():
    c = cfg()
    c["settings"].update(allocation_mode=SECTION_PCT, allocation_pct=10)
    state = initial_state(500)
    state["positions"]["one"] = open_position(
        "BTCUSDT", margin=30, budget=150, leverage=3)
    state["pending"]["two"] = {"symbol": "BTCUSDT", "margin": 25}
    assert capital_margin(c, state, c["settings"], symbol="BTCUSDT") == 50


@pytest.mark.parametrize("mode,expect", [
    (SECTION_PCT, 50.0), (COIN_PCT, 50.0), (COIN_FIXED, 35.0),
])
def test_initial_allocation_and_labels_are_consistent(mode, expect):
    c = cfg()
    c["settings"] = section_policy(mode)
    c = validate(c)
    margin = capital_margin(c, initial_state(500), c["settings"],
                            symbol="BTCUSDT")
    assert margin == pytest.approx(expect)
    preview = grid_preview_data(c, "BTCUSDT", c["sections"][0]["id"])
    assert f"{expect:g} USDT" in preview["status"]
    reviewed = effective_risk_review_text(c)
    assert f"{expect:g} USDT" in reviewed
    assert any(k == "Распределение" for k, _ in summary_rows(c))


@pytest.mark.parametrize("mode", [COIN_PCT, COIN_FIXED])
def test_coin_budget_subtracts_planned_open_and_pending_ranges(mode):
    c = cfg()
    c["settings"] = section_policy(mode, pct=10, fixed=50)
    state = initial_state(500)
    state["positions"]["section-a"] = open_position(
        "BTCUSDT", margin=8, order_margin=2, budget=60, leverage=3)
    state["pending"]["section-b"] = {"symbol": "BTCUSDT", "margin": 5}
    assert occupied_coin_margin(state, "BTCUSDT") == pytest.approx(25)
    assert capital_margin(c, state, c["settings"], symbol="BTCUSDT") == pytest.approx(25)


def test_coin_budget_denies_second_full_grid_on_same_coin_but_not_other():
    c = cfg()
    c["settings"] = section_policy(COIN_FIXED, fixed=25)
    state = initial_state(500)
    state["positions"]["open-btc"] = open_position(
        "BTCUSDT", margin=4, order_margin=1, budget=75, leverage=3)
    assert capital_margin(c, state, c["settings"], symbol="BTCUSDT") == 0
    assert capital_margin(c, state, c["settings"], symbol="ETHUSDT") == 25


def test_more_than_19_positions_is_not_a_fixed_software_cap():
    c = cfg()
    c["settings"] = section_policy(COIN_FIXED, fixed=100)
    state = initial_state(500)
    for index in range(32):
        state["positions"][f"other-{index}"] = open_position(
            "OTHERUSDT", margin=1, budget=2, leverage=2)
    assert capital_margin(c, state, c["settings"], symbol="BTCUSDT") == 100


def test_exposure_is_conservative_when_active_orders_are_less_than_full_grid():
    state = initial_state(500)
    state["positions"]["a"] = open_position(
        "BTCUSDT", margin=7, order_margin=3, budget=40, leverage=2)
    assert occupied_coin_margin(state, "BTCUSDT") == 20


def test_partial_coin_budget_must_not_round_into_a_microscopic_order():
    state = initial_state(500)
    state["pending"]["a"] = {"symbol": "BTCUSDT", "margin": 49.9999999999}
    assert available_margin_for_coin(
        state, "BTCUSDT", 50.0, section_policy(COIN_PCT)) == 0


@pytest.mark.parametrize("corrupt", [
    {"budget": math.nan, "settings": {"leverage": 3}},
    {"budget": 90, "settings": {"leverage": 0}},
    {"margin": -1},
    {"order_margin": "10"},
])
def test_corrupt_coin_exposure_cannot_free_capacity(corrupt):
    c = cfg()
    c["settings"] = section_policy(COIN_FIXED, fixed=50)
    state = initial_state(500)
    position = open_position("BTCUSDT", margin=5, budget=30, leverage=3)
    position.update(corrupt)
    state["positions"]["bad"] = position
    assert capital_margin(c, state, c["settings"], symbol="BTCUSDT") == 0


@pytest.mark.parametrize("bad", [False, -0.01, math.nan, math.inf, "25", 1e10])
def test_fixed_amount_rejected_by_full_schema(bad):
    c = cfg()
    c["settings"] = section_policy(COIN_FIXED)
    c["settings"]["allocation_fixed_usdt"] = bad
    with pytest.raises(ValueError):
        validate(c)


@pytest.mark.parametrize("bad_mode", ["fixed", "percent", "", None, [], 15])
def test_unknown_modes_never_fall_back_to_legacy(bad_mode):
    c = cfg()
    c["settings"]["allocation_mode"] = bad_mode
    with pytest.raises(ValueError):
        validate(c)


def test_nonexistent_symbol_or_missing_state_denies_coin_spending():
    c = cfg()
    c["settings"] = section_policy(COIN_FIXED)
    assert capital_margin(c, initial_state(500), c["settings"],
                          symbol="SOLUSDT") == 0
    assert capital_margin(c, {}, c["settings"], symbol="BTCUSDT") == 0
    assert capital_margin(c, initial_state(500), c["settings"]) == 0


def test_coin_override_does_not_change_other_coin_or_sections():
    c = cfg()
    c["coin_overrides"]["ETHUSDT"] = {
        "settings": {"allocation_mode": COIN_FIXED, "allocation_fixed_usdt": 20}}
    saved = validate_constructor_roundtrip(c)
    b = effective(saved, "BTCUSDT", saved["sections"][0])
    e = effective(saved, "ETHUSDT", saved["sections"][1])
    assert b["allocation_mode"] == SECTION_PCT
    assert e["allocation_mode"] == COIN_FIXED
    assert planned_margin(saved["capital"], e) == 20
    assert planned_margin(saved["capital"], b) == 25


def test_one_coin_can_override_global_coin_fixed_cap_consistently():
    c = cfg()
    c["settings"].update(allocation_mode=COIN_FIXED, allocation_fixed_usdt=30)
    c["coin_overrides"]["ETHUSDT"] = {
        "settings": {"allocation_fixed_usdt": 45}}
    out = validate_constructor_roundtrip(c)
    assert all(effective(out, "BTCUSDT", sec)["allocation_fixed_usdt"] == 30
               for sec in out["sections"])
    assert all(effective(out, "ETHUSDT", sec)["allocation_fixed_usdt"] == 45
               for sec in out["sections"])


def test_mixed_coin_quota_and_per_section_mode_must_fail_closed():
    c = cfg()
    c["sections"][0]["overrides"] = {
        "allocation_mode": COIN_FIXED, "allocation_fixed_usdt": 18}
    with pytest.raises(ValueError, match="общая квота"):
        validate(c)


def test_coin_quota_per_section_override_conflict_rejected():
    c = cfg()
    c["settings"].update(allocation_mode=COIN_PCT, allocation_pct=12)
    sid = c["sections"][0]["id"]
    c["coin_overrides"]["BTCUSDT"] = {
        "sections": {sid: {"settings": {"allocation_pct": 15}}}}
    with pytest.raises(ValueError, match="общая квота"):
        validate(c)


def test_inactive_percent_differences_do_not_change_fixed_coin_cap():
    c = cfg()
    c["settings"].update(allocation_mode=COIN_FIXED, allocation_fixed_usdt=25)
    c["sections"][0]["overrides"] = {"allocation_pct": 30}
    checked = validate(c)
    assert allocation_signature(
        effective(checked, "BTCUSDT", checked["sections"][0])
    ) == (COIN_FIXED, 25)


def test_active_coin_budget_summary_change_is_visible_to_operator():
    base = cfg()
    edited = deepcopy(base)
    edited["settings"].update(allocation_mode=COIN_FIXED,
                              allocation_fixed_usdt=40)
    summary = constructor_change_summary(validate(base), validate(edited))
    assert summary["risk_changed"] > 0


@pytest.mark.parametrize("mode", [COIN_PCT, COIN_FIXED])
def test_reinvestment_only_changes_percentage_not_fixed_budget(mode):
    c = cfg()
    c["settings"] = section_policy(mode, pct=10, fixed=40)
    c["reinvest"] = True
    state = initial_state(500)
    state["strategy"]["veles_reinvested"] = 100
    amount = capital_margin(c, state, c["settings"], symbol="BTCUSDT")
    assert amount == pytest.approx(60 if mode == COIN_PCT else 40)


def test_randomized_coin_allocation_never_exceeds_remaining_budget():
    randomizer = random.Random(20261118)
    c = cfg()
    c["settings"] = section_policy(COIN_FIXED, fixed=150)
    for case in range(100):
        state = initial_state(500)
        count = randomizer.randrange(0, 50)
        used = []
        for i in range(count):
            principal = randomizer.uniform(0.01, 12.0)
            state["pending"][str(i)] = {"symbol": "BTCUSDT",
                                        "margin": principal}
            used.append(principal)
        margin = capital_margin(c, state, c["settings"], symbol="BTCUSDT")
        assert margin >= 0
        assert margin <= max(0.0, 150.0 - math.fsum(used)) + 1e-8
        assert margin + math.fsum(used) <= max(150.0, math.fsum(used)) + 1e-8
