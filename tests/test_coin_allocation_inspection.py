"""Architecture regression: every operator-facing money field is a real setting.

The same VELES capital policy is visible in the constructor, frozen/current
inspection, effective-risk audit, and PAPER executor. No placeholder controls.
"""
from copy import deepcopy

from bcc.config import defaults, effective, validate
from bcc.coin_allocation import planned_margin
from bcc.constructor_review import effective_risk_review_text
from bcc.inspection import NAMES, changes
from bcc.widgets import FINANCE
from bcc.wizard_presentation import grid_preview_data, summary_rows


def test_every_new_capital_field_is_exposed_in_native_form_and_inspection():
    values = defaults("VELES")["settings"]
    finance_names = [spec[0] for spec in FINANCE]
    for key in ("allocation_mode", "allocation_pct", "allocation_fixed_usdt"):
        assert key in values
        assert finance_names.count(key) == 1
        assert key in NAMES


def test_coin_budget_is_visible_in_preview_audit_and_stored_change_history():
    cfg = defaults("VELES")
    cfg["name"] = "inspection-test"
    cfg["coin_overrides"]["ETHUSDT"] = {
        "settings": {"allocation_mode": "coin_fixed",
                     "allocation_fixed_usdt": 77.0},
    }
    cfg = validate(cfg)
    eff = effective(cfg, "ETHUSDT", cfg["sections"][0])
    assert planned_margin(cfg["capital"], eff) == 77.0
    preview = grid_preview_data(cfg, "ETHUSDT", cfg["sections"][0]["id"])
    assert "77 USDT" in preview["status"]
    assert "Общая маржинальная квота" in preview["status"]
    review = effective_risk_review_text(cfg, sample_limit=100)
    assert "77 USDT" in review
    assert any(field == "Распределение" for field, _ in summary_rows(cfg))

    updated = deepcopy(cfg)
    updated["coin_overrides"]["ETHUSDT"]["settings"][
        "allocation_fixed_usdt"] = 83.0
    diff = changes(cfg, updated)
    assert any("Фиксированная квота" in item for item in diff)


def test_unchanged_legacy_effective_review_uses_old_terminology():
    cfg = defaults("VELES")
    cfg["name"] = "legacy-review-contract"
    review = effective_risk_review_text(cfg)
    assert "условная маржа 25 USDT; номинал 75 USDT" in review
    assert "плановая маржа" not in review


def test_shared_coin_allocation_is_not_double_counted_per_section_in_preview():
    cfg = defaults("VELES")
    cfg["name"] = "shared allocation preview"
    cfg["settings"].update(allocation_mode="coin_pct", allocation_pct=8)
    cfg = validate(cfg)
    section = cfg["sections"][0]
    preview = grid_preview_data(cfg, "BTCUSDT", section["id"])
    assert "40 USDT" in preview["status"]
    assert "до учёта открытых диапазонов" in preview["status"]
    assert "общая квота монеты" in effective_risk_review_text(cfg)
