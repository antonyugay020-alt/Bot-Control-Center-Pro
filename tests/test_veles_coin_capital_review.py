"""Pure effective coin-capital change review and explicit Windows risk audit.

No worker, exchange, SQLite or GUI side effects in this file.
"""
from copy import deepcopy

from bcc.config import defaults
from bcc.constructor_contract import project_veles_coin_transaction
from bcc.constructor_review import (
    constructor_change_summary, constructor_change_review_text,
    effective_risk_review_text,
)


def sample():
    cfg = defaults("VELES")
    cfg["name"] = "Per-coin capital review"
    cfg["symbols"] = ["BTCUSDT", "ETHUSDT"]
    return cfg


def test_coin_budget_only_change_is_visible_for_every_affected_section():
    before = sample()
    changed = project_veles_coin_transaction(before, coin_rules={
        "BTCUSDT": {"capital_limit": {"mode": "fixed", "value": 60.0}},
    })
    old_copy = deepcopy(before)
    after_copy = deepcopy(changed)
    report = constructor_change_summary(before, changed)
    assert report["coin_budget_changed"] == 1
    assert report["coin_overrides_changed"] == 1
    assert report["combinations_modified"] == len(before["sections"])
    assert report["risk_changed"] == len(before["sections"])
    assert report["signals_changed"] == 0
    assert report["execution_changed"] == 0
    assert report["orders_changed"] == 0
    assert report["exits_changed"] == 0
    assert before == old_copy and changed == after_copy
    text = constructor_change_review_text(before, changed)
    assert "Изменены общие лимиты капитала монет: 1" in text
    assert "Риск и маржа: 2" in text
    preview = effective_risk_review_text(changed, sample_limit=20)
    assert "BTCUSDT" in preview
    assert "общий предел монеты 60 USDT" in preview
    assert "общий предел монеты не задан" in preview


def test_one_coin_cap_override_does_not_change_another_coin_review():
    before = sample()
    changed = deepcopy(before)
    changed["coin_overrides"]["ETHUSDT"] = {
        "capital_limit": {"mode": "percent", "value": 12.5},
    }
    report = constructor_change_summary(before, changed)
    assert report["coin_budget_changed"] == 1
    assert report["combinations_modified"] == len(before["sections"])
    assert all(s.startswith("ETHUSDT/") for s in report["samples"])


def test_inherited_global_deposit_change_updates_effective_percent_cap():
    before = sample()
    before["coin_overrides"]["BTCUSDT"] = {
        "capital_limit": {"mode": "percent", "value": 15},
    }
    after = deepcopy(before)
    after["capital"] = 600
    report = constructor_change_summary(before, after)
    assert report["coin_budget_changed"] == 1
    assert report["risk_changed"] == len(before["sections"])
    assert "общий предел монеты 90 USDT" in effective_risk_review_text(after)


def test_fixed_cap_does_not_change_when_global_deposit_changes():
    before = sample()
    before["coin_overrides"]["BTCUSDT"] = {
        "capital_limit": {"mode": "fixed", "value": 45},
    }
    after = deepcopy(before)
    after["capital"] = 600
    report = constructor_change_summary(before, after)
    assert report["coin_budget_changed"] == 0
    assert "общий предел монеты 45 USDT" in effective_risk_review_text(after)


def test_preview_flags_section_larger_than_coin_cap():
    cfg = sample()
    cfg["coin_overrides"]["BTCUSDT"] = {
        "capital_limit": {"mode": "fixed", "value": 20.0},
    }
    report = effective_risk_review_text(cfg, sample_limit=20)
    assert "BTCUSDT" in report
    assert "общий предел монеты 20 USDT" in report
    assert report.count("вход заблокирован: одна секция превышает предел монеты") == len(cfg["sections"])
    assert "общий предел монеты не задан" in report


def test_polymarket_review_does_not_inherit_veles_coin_caps():
    before = defaults("POLYMARKET")
    before["name"] = "Polymarket independent review"
    after = deepcopy(before)
    after["polymarket"]["stake_pct"] = 7
    report = constructor_change_summary(before, after)
    assert report["coin_budget_changed"] == 0
    assert report["stake_changed"] == (
        len(before["symbols"]) * len(before["polymarket"]["durations"])
    )
