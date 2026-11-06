"""Selector-independent constructor review and bounded effective risk coverage."""
from copy import deepcopy

import pytest

from bcc.config import defaults
from bcc.constructor_review import (
    constructor_review_text,
    effective_risk_review_text,
)


def configured_veles():
    cfg = defaults("VELES")
    cfg.update(name="Risk audit", symbols=["ETHUSDT", "BTCUSDT"])
    section_id = cfg["sections"][0]["id"]
    cfg["coin_overrides"]["ETHUSDT"] = {
        "settings": {"allocation_pct": 10, "leverage": 7},
        "sections": {section_id: {"side": "SHORT", "enabled": False}},
    }
    return cfg


def test_review_risk_resolves_per_coin_side_margin_stop_and_admission():
    cfg = configured_veles()
    before = deepcopy(cfg)
    report = effective_risk_review_text(cfg, sample_limit=100)
    assert "BTCUSDT / " in report
    assert "ETHUSDT / " in report
    assert "SHORT, CROSS, плечо 7x" in report
    assert "условная маржа 50 USDT; номинал 350 USDT" in report
    assert "вход отключён" in report
    assert cfg == before


def test_review_does_not_depend_on_navigation_or_order_of_symbols_and_sections():
    cfg = configured_veles()
    before = deepcopy(cfg)
    report = constructor_review_text(cfg, cfg, include_change_audit=True)
    reordered = deepcopy(cfg)
    reordered["symbols"].reverse()
    reordered["sections"].reverse()
    assert constructor_review_text(reordered, reordered, include_change_audit=True) == report
    assert "Изменения относительно открытой конфигурации" in report
    assert "Примеры эффективных параметров риска" in report
    assert cfg == before


def test_risk_review_bounds_display_not_number_of_accounts():
    cfg = configured_veles()
    cfg["symbols"] = [f"COIN{i:05d}USDT" for i in range(1200)]
    text = effective_risk_review_text(cfg, sample_limit=2)
    total = len(cfg["symbols"]) * len(cfg["sections"])
    assert f"до 2 из {total} диапазонов" in text
    assert text.count("условная маржа") == 2
    assert f"Остальные {total - 2} диапазонов" in text


def test_review_excluded_overrides_and_polymarket_have_no_grid_selection():
    cfg = defaults("POLYMARKET")
    report = constructor_review_text(cfg, cfg, include_change_audit=True,
                                     dropped_overrides=("BTC:300", "ETH:900"))
    assert "BTC:300, ETH:900" in report
    assert "Изменения относительно открытой конфигурации" in report
    assert "Примеры эффективных параметров риска" not in report


@pytest.mark.parametrize("limit", [-1, 101, True, 1.5])
def test_risk_review_rejects_invalid_sample_limit(limit):
    with pytest.raises(ValueError):
        effective_risk_review_text(configured_veles(), sample_limit=limit)


def test_risk_review_rejects_wrong_strategy():
    with pytest.raises(ValueError):
        effective_risk_review_text(defaults("POLYMARKET"))
