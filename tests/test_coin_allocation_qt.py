"""Modern Qt allocation controls map to exactly the persisted worker fields.

Runs with an offscreen Qt application in the repository acceptance suite.
No Windows trading instances, connectors or live bot accounts are used.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from bcc.config import defaults, effective, validate
from bcc.widgets import SettingsEditor
from bcc.wizard_presentation import grid_preview_data, summary_rows


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def selected(editor):
    return editor.finance.controls["allocation_mode"][1].currentData()


def test_legacy_mode_is_selected_on_fresh_wizard(qapp):
    ed = SettingsEditor(defaults("VELES")["settings"], strategy="VELES")
    assert selected(ed) == "section_pct"
    assert ed.finance.controls["allocation_pct"][1].isEnabled()
    assert not ed.finance.controls["allocation_fixed_usdt"][1].isEnabled()
    assert ed.value_data()["allocation_mode"] == "section_pct"
    ed.deleteLater()


def test_standalone_legacy_settings_editor_still_opens_losslessly(qapp):
    old = defaults("VELES")["settings"]
    old.pop("allocation_mode")
    old.pop("allocation_fixed_usdt")
    ed = SettingsEditor(old, strategy="VELES")
    saved = ed.value_data()
    assert saved["allocation_mode"] == "section_pct"
    assert saved["allocation_fixed_usdt"] == 25.0
    assert saved["allocation_pct"] == old["allocation_pct"]
    assert "allocation_mode" not in old
    ed.deleteLater()


def test_fixed_mode_edit_updates_real_worker_settings_and_disables_percent(qapp):
    ed = SettingsEditor(defaults("VELES")["settings"], strategy="VELES")
    mode = ed.finance.controls["allocation_mode"][1]
    fixed = ed.finance.controls["allocation_fixed_usdt"][1]
    percent = ed.finance.controls["allocation_pct"][1]
    mode.setCurrentIndex(mode.findData("coin_fixed"))
    assert selected(ed) == "coin_fixed"
    assert fixed.isEnabled() and not percent.isEnabled()
    fixed.setValue(37.5)
    data = ed.value_data()
    assert data["allocation_mode"] == "coin_fixed"
    assert data["allocation_fixed_usdt"] == pytest.approx(37.5)
    assert data["allocation_pct"] == 5.0
    ed.deleteLater()


def test_switching_modes_preserves_inactive_input_without_changing_policy(qapp):
    ed = SettingsEditor(defaults("VELES")["settings"], strategy="VELES")
    mode = ed.finance.controls["allocation_mode"][1]
    fixed = ed.finance.controls["allocation_fixed_usdt"][1]
    percent = ed.finance.controls["allocation_pct"][1]
    mode.setCurrentIndex(mode.findData("coin_fixed"))
    fixed.setValue(88.0)
    mode.setCurrentIndex(mode.findData("coin_pct"))
    assert percent.isEnabled() and not fixed.isEnabled()
    percent.setValue(12.0)
    value = ed.value_data()
    assert value["allocation_pct"] == pytest.approx(12)
    assert value["allocation_fixed_usdt"] == pytest.approx(88)
    assert value["allocation_mode"] == "coin_pct"
    ed.deleteLater()


def test_non_section_strategies_do_not_show_inapplicable_coin_budget_ui(qapp):
    for kind in ("GRID", "PUMP_DUMP", "POLYMARKET"):
        ed = SettingsEditor(defaults(kind)["settings"], strategy=kind)
        for key in ("allocation_mode", "allocation_pct", "allocation_fixed_usdt"):
            assert key not in ed.finance.controls
        # The full persisted settings still preserve these harmless,
        # inactive fields because the original JSON schema is common.
        assert ed.value_data()["allocation_mode"] == "section_pct"
        ed.deleteLater()


def test_coin_specific_mode_inherited_by_individual_editor(qapp):
    c = defaults("VELES")
    c["name"] = "per coin Qt"
    c["coin_overrides"]["ETHUSDT"] = {
        "settings": {"allocation_mode": "coin_fixed",
                     "allocation_fixed_usdt": 19.0}}
    c = validate(c)
    inherited = effective(c, "ETHUSDT", c["sections"][0])
    ed = SettingsEditor(inherited, strategy="VELES")
    assert selected(ed) == "coin_fixed"
    assert ed.value_data()["allocation_fixed_usdt"] == pytest.approx(19)
    ed.deleteLater()
    preview = grid_preview_data(c, "ETHUSDT", c["sections"][0]["id"])
    assert "19 USDT" in preview["status"]
    assert "Общая маржинальная квота" in preview["status"]
    assert any(key == "Распределение" for key, _ in summary_rows(c))
