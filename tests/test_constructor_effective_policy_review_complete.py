"""Effective policy audit for every constructor field; no worker or network."""
from copy import deepcopy
import random

import pytest

from bcc.config import defaults, poly_rule_keys, resolve_poly_market, effective
from bcc.constructor_review import (
    POLY_REVIEW_FIELDS, VELES_REVIEW_FIELDS,
    constructor_change_summary, constructor_change_review_text,
)
from bcc.constructor_contract import validate_constructor_roundtrip
from bcc.storage import Repository


def _poly():
    cfg = defaults("POLYMARKET")
    cfg.update(name="Policy audit", symbols=["BTC", "ETH", "SOL"])
    cfg["polymarket"].update(durations=[300, 900], impulse_mode="usd",
        impulse=85.0, entry_signal_mode="before_close",
        entry_seconds_before_close=80, entry_window_seconds=15,
        entry_min_seconds_left=5)
    return cfg


def _veles():
    cfg = defaults("VELES")
    cfg.update(name="Risk audit", symbols=["BTCUSDT", "ETHUSDT"])
    return cfg


def test_review_schema_exhaustive_and_nonoverlapping():
    assert set().union(*POLY_REVIEW_FIELDS.values()) == poly_rule_keys()
    assert sum(map(len, POLY_REVIEW_FIELDS.values())) == len(poly_rule_keys())
    assert set().union(*VELES_REVIEW_FIELDS.values()) == set(defaults("VELES")["settings"])
    assert sum(map(len, VELES_REVIEW_FIELDS.values())) == len(defaults("VELES")["settings"])


@pytest.mark.parametrize("field,value,category", [
    ("entry_signal_mode", "anytime", "entry_policy_changed"),
    ("entry_seconds_before_close", 70, "entry_policy_changed"),
    ("entry_window_seconds", 10, "entry_policy_changed"),
    ("entry_min_seconds_left", 7, "entry_policy_changed"),
    ("impulse", 90, "impulse_changed"),
    ("impulse_mode", "percent", "impulse_changed"),
    ("ask_min", 0.2, "ask_changed"),
    ("ask_max", 0.7, "ask_changed"),
    ("stake_pct", 6.0, "stake_changed"),
    ("fee_mode", "estimate", "fees_changed"),
    ("fee_rate", 0.01, "fees_changed"),
    ("disabled", True, "availability_changed"),
])
def test_poly_every_persisted_scalar_change_visible(field, value, category):
    original = _poly()
    proposed = deepcopy(original)
    proposed["polymarket"][field] = value
    assert validate_constructor_roundtrip(proposed)
    before = deepcopy(proposed)
    report = constructor_change_summary(original, proposed)
    assert report[category] == 6
    assert report["combinations_modified"] == 6
    assert proposed == before


def test_poly_indicator_filter_category_and_inheritance(tmp_path):
    original = _poly()
    proposed = deepcopy(original)
    # The filter can be configured on the global level; all markets inherit it.
    proposed["polymarket"]["indicator_filter"] = {
        "operator": "AND", "conditions": [{
            "indicator": "RSI", "period": 14, "timeframe": 5,
            "trigger": "bar_close", "comparison": ">", "value": 50,
            "reference": "number", "output": "value", "params": {},
        }]}
    # Polymarket filters use a separate schema; report must still be pure.
    report = constructor_change_summary(original, proposed)
    assert report["signals_changed"] == 6
    assert report["combinations_modified"] == 6
    assert "индикаторные фильтры" in constructor_change_review_text(original, proposed)


def test_poly_shadowed_global_change_only_counts_effective_markets(tmp_path):
    original = _poly()
    original["polymarket"]["overrides"]["BTC:300"] = {
        "entry_seconds_before_close": 40}
    proposed = deepcopy(original)
    proposed["polymarket"]["entry_seconds_before_close"] = 70
    report = constructor_change_summary(original, proposed)
    assert report["entry_seconds_changed"] == 5
    assert report["entry_policy_changed"] == 5
    folder = Repository(tmp_path).create(proposed)
    restored, _ = Repository(tmp_path).load(folder)
    assert constructor_change_summary(original, restored) == report


@pytest.mark.parametrize("seed", range(60))
def test_poly_random_market_changes_match_exact_effective_policy(seed):
    rng = random.Random(2026110521 + seed)
    original = _poly()
    proposed = deepcopy(original)
    symbol = rng.choice(original["symbols"])
    duration = rng.choice(original["polymarket"]["durations"])
    field, value, category = rng.choice([
        ("entry_seconds_before_close", 70, "entry_policy_changed"),
        ("entry_window_seconds", 12, "entry_policy_changed"),
        ("ask_min", 0.2, "ask_changed"),
        ("impulse", 92, "impulse_changed"),
        ("stake_pct", 6, "stake_changed"),
        ("disabled", True, "availability_changed"),
    ])
    proposed["polymarket"]["overrides"][f"{symbol}:{duration}"] = {field: value}
    assert validate_constructor_roundtrip(proposed)
    report = constructor_change_summary(original, proposed)
    assert report["combinations_modified"] == 1
    assert report[category] == 1
    assert report["samples"] == [f"{symbol}:{duration}"]


@pytest.mark.parametrize("path,value,category", [
    (("leverage",), 4, "risk_changed"),
    (("margin_mode",), "ISOLATED", "risk_changed"),
    (("allocation_pct",), 8, "risk_changed"),
    (("orders", "count"), 7, "orders_changed"),
    (("profit", "pct"), 1.5, "exits_changed"),
    (("stop", "pct"), 2, "exits_changed"),
    (("fee_rate",), 0.001, "execution_changed"),
    (("slippage_pct",), 0.03, "execution_changed"),
])
def test_veles_risk_orders_and_exits_reported(path, value, category):
    original = _veles()
    proposed = deepcopy(original)
    obj = proposed["settings"]
    for key in path[:-1]:
        obj = obj[key]
    obj[path[-1]] = value
    report = constructor_change_summary(original, proposed)
    assert report[category] == 4
    assert report["combinations_modified"] == 4
    assert category in report


def test_veles_shadowed_section_overrides_not_falsely_reported():
    original = _veles()
    for symbol in original["symbols"]:
        original["coin_overrides"][symbol] = {"sections": {
            original["sections"][0]["id"]: {"settings": {"leverage": 5}}
        }}
    proposed = deepcopy(original)
    proposed["sections"][0]["overrides"]["leverage"] = 6
    report = constructor_change_summary(original, proposed)
    assert report["combinations_modified"] == 0
    assert report["risk_changed"] == 0


def test_veles_section_status_and_signals_reported_separately():
    original = _veles()
    proposed = deepcopy(original)
    proposed["sections"][0]["enabled"] = False
    proposed["sections"][1]["exit"]["conditions"][0]["timeframe"] = 5
    report = constructor_change_summary(original, proposed)
    assert report["combinations_modified"] == 4
    assert report["section_state_changed"] == 2
    assert report["signals_changed"] == 2
    assert "состояние секций" in constructor_change_review_text(original, proposed)


def test_review_fails_closed_on_new_unclassified_policy(monkeypatch):
    monkeypatch.setattr("bcc.config.poly_rule_keys", lambda: poly_rule_keys() | {"new_order_field"})
    with pytest.raises(ValueError, match="Неполный перечень"):
        constructor_change_summary(_poly(), _poly())
