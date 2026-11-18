"""Low-level PAPER executor and strategy share EXACT persisted coin policy.

No market books here, so even a valid entry stops at the separate book gate.
No exchange or persistent account is touched.
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
    checked = validate(cfg)
    state = initial_state(500)
    events = []
    settings = deepcopy(checked["settings"])
    orders = [{"price": 100.0, "weight": 1.0}]
    return state, events, settings, orders


def source_config(settings):
    cfg = defaults("VELES")
    cfg["name"] = "source policy"
    cfg["settings"] = deepcopy(settings)
    return validate(cfg)


def attempt(state, events, settings, orders, **kwargs):
    source = source_config(settings)
    section = source["sections"][0]["id"]
    return enter(
        state, events, "BTCUSDT:" + section, "BTCUSDT", "LONG",
        settings, 100.0, 20.0, orders, section, **kwargs,
    )


def test_direct_coin_entry_requires_source_policy_config():
    state, events, settings, orders = inputs()
    before = deepcopy(state)
    success, reason = attempt(state, events, settings, orders)
    assert not success
    assert "Не подтверждена конфигурация квоты" in reason
    assert state == before
    assert events == []


def test_executor_blocks_oversubscribed_coin_before_any_mutation():
    state, events, settings, orders = inputs()
    state["pending"]["other-section"] = {
        "symbol": "BTCUSDT", "margin": 10.0,
    }
    before = deepcopy(state)
    success, reason = attempt(
        state, events, settings, orders, coin_config=source_config(settings),
    )
    assert not success
    assert "Общая квота монеты BTCUSDT" in reason
    assert "15 USDT" in reason
    assert state == before
    assert events == []


@pytest.mark.parametrize("source", [None, -1, 0, True, [], {}, {"symbols": []}])
def test_untrusted_config_never_enters_position(source):
    state, events, settings, orders = inputs()
    success, reason = attempt(
        state, events, settings, orders, coin_config=source)
    assert not success
    assert "квота" in reason.lower()
    assert not events
    assert not state["positions"]


def test_fixed_coin_budget_cannot_be_inflated_by_numeric_argument():
    state, events, settings, orders = inputs()
    with pytest.raises(TypeError):
        attempt(state, events, settings, orders, coin_budget=100000.0)
    assert not state["positions"]
    assert not events


def test_source_config_cannot_supply_different_fixed_margin():
    state, events, settings, orders = inputs()
    forged = source_config(settings)
    forged["settings"]["allocation_fixed_usdt"] = 100000.0
    before = deepcopy(state)
    success, reason = attempt(
        state, events, settings, orders, coin_config=forged,
    )
    assert not success
    assert "не совпадает с настройками секции" in reason
    assert state == before
    assert not events


def test_source_config_cannot_supply_another_currency_or_section():
    state, events, settings, orders = inputs()
    forged = source_config(settings)
    forged["symbols"] = ["ETHUSDT"]
    success, reason = attempt(
        state, events, settings, orders, coin_config=forged,
    )
    assert not success
    assert "Не подтверждена конфигурация квоты" in reason
    assert not state["positions"]


def test_source_bound_valid_coin_cap_reaches_separate_book_guard():
    state, events, settings, orders = inputs()
    success, reason = attempt(
        state, events, settings, orders, coin_config=source_config(settings),
    )
    assert not success
    assert "стакана" in reason
    assert "квота" not in reason.lower()
    assert not events


def test_percentage_cap_is_recomputed_from_persisted_capital():
    state, events, settings, orders = inputs()
    settings["allocation_mode"] = "coin_pct"
    settings["allocation_pct"] = 3.0  # 15 USDT on 500
    success, reason = attempt(
        state, events, settings, orders, coin_config=source_config(settings),
    )
    assert not success
    assert "доступно 15 USDT вместо 20 USDT" in reason
    assert not events


def test_existing_pending_grid_is_not_charged_for_its_reservation_twice():
    state, events, settings, orders = inputs()
    success, reason = attempt(
        state, events, settings, orders, existing_grid=True)
    assert not success
    assert "стакана" in reason
    assert "квота" not in reason.lower()
    assert not events


def test_legacy_mode_keeps_old_entry_path_without_coin_config():
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
