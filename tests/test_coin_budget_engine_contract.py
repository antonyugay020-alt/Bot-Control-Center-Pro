"""VELES coin budget is one margin ceiling shared by all ranges and DCA."""
from copy import deepcopy

import pytest

from bcc.config import condition, defaults, effective, group, validate
from bcc.coin_allocation import committed_coin_margin, remaining_coin_budget
from bcc.constructor_contract import validate_constructor_roundtrip
from bcc.strategies import Veles, capital_margin
from bcc.storage import initial_state
from test_core import attach, bars


def example():
    cfg = defaults("VELES")
    cfg["name"] = "Coin budget"
    cfg["settings"].update(leverage=1, allocation_pct=10, allocation_usdt=75,
                           fee_rate=.001, slippage_pct=0)
    cfg["settings"]["orders"]["count"] = 1
    cfg["sections"] = cfg["sections"][:1]
    cfg["sections"][0]["entry"] = group(condition("PRICE", 0, ">", 1))
    cfg["sections"][0]["exit"] = group(condition("PRICE", 1e9, ">", 1))
    return cfg


def test_previous_section_percentage_survives_import():
    cfg = example()
    del cfg["settings"]["allocation_mode"]
    del cfg["settings"]["allocation_usdt"]
    restored = validate(cfg)
    assert restored["settings"]["allocation_mode"] == "section_pct"
    assert capital_margin(restored, initial_state(500), restored["settings"], "BTCUSDT") == 50


@pytest.mark.parametrize("mode,amount,expected", [
    ("coin_pct", 10, 50), ("coin_pct", 3.5, 17.5),
    ("coin_fixed", 75, 75), ("coin_fixed", 32.5, 32.5),
])
def test_coin_percent_and_fixed_margin_modes(mode, amount, expected):
    cfg = example()
    cfg["settings"]["allocation_mode"] = mode
    cfg["settings"]["allocation_pct" if mode == "coin_pct" else "allocation_usdt"] = amount
    assert validate_constructor_roundtrip(cfg)
    state = initial_state(500)
    assert capital_margin(cfg, state, cfg["settings"], "BTCUSDT") == pytest.approx(expected)


def test_mixed_coin_overrides_and_inheritance():
    cfg = example()
    cfg["symbols"] = ["BTCUSDT", "ETHUSDT"]
    cfg["settings"]["allocation_mode"] = "coin_pct"
    cfg["settings"]["allocation_pct"] = 5
    cfg["coin_overrides"] = {
        "BTCUSDT": {"settings": {"allocation_mode": "coin_fixed", "allocation_usdt": 90}},
        "ETHUSDT": {"settings": {"allocation_pct": 2}},
    }
    assert validate_constructor_roundtrip(cfg)
    state = initial_state(500)
    assert capital_margin(cfg, state, effective(cfg, "BTCUSDT"), "BTCUSDT") == 90
    assert capital_margin(cfg, state, effective(cfg, "ETHUSDT"), "ETHUSDT") == 10


def test_aggregate_reserves_include_dca_and_pending():
    cfg = example()
    cfg["settings"]["allocation_mode"] = "coin_fixed"
    cfg["settings"]["allocation_usdt"] = 40
    state = initial_state(500)
    state["positions"]["one"] = {"symbol": "BTCUSDT", "margin": 12, "order_margin": 15}
    state["pending"]["two"] = {"symbol": "BTCUSDT", "margin": 8}
    assert committed_coin_margin(state, "BTCUSDT") == 35
    assert remaining_coin_budget(cfg, state, "BTCUSDT") == 5


def test_simultaneous_ranges_do_not_double_spend_one_coin_budget():
    cfg = example()
    cfg["settings"].update(allocation_mode="coin_fixed", allocation_usdt=60)
    second = deepcopy(cfg["sections"][0])
    second["id"] = "SECOND_RANGE"
    second["label"] = "SECOND"
    cfg["sections"].append(second)
    assert validate(cfg)
    state, events = initial_state(500), []
    attach(state)
    Veles().process(cfg, state, events, "BTCUSDT", bars([100, 100]),
                    99.99, 100.01, 120000)
    assert len(state["positions"]) == 1
    assert committed_coin_margin(state, "BTCUSDT") == pytest.approx(60)
    assert capital_margin(cfg, state, cfg["settings"], "BTCUSDT") == 0
    assert capital_margin(cfg, state, cfg["settings"], "ETHUSDT") == 60


@pytest.mark.parametrize("bad", [0, -10, float("nan")])
def test_invalid_fixed_budget_fails_closed(bad):
    cfg = example()
    cfg["settings"]["allocation_mode"] = "coin_fixed"
    cfg["settings"]["allocation_usdt"] = bad
    with pytest.raises(ValueError):
        validate(cfg)


def test_section_cannot_redefine_coin_budget():
    cfg = example()
    cfg["settings"]["allocation_mode"] = "coin_fixed"
    cfg["sections"][0]["overrides"]["allocation_mode"] = "coin_pct"
    with pytest.raises(ValueError, match="Бюджет монеты"):
        validate(cfg)
