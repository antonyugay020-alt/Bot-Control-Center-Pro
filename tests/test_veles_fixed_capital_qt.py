"""Modern Qt VELES fixed-USDT allocation create/edit/reopen acceptance.

Runs in native Windows QA, offscreen Qt does not certify Windows E2E.
No trading workers, orders or user bot databases are accessed.
"""
import os
if os.name != "nt" and not os.environ.get("DISPLAY"):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
import pytest

from bcc.config import defaults, effective
from bcc.storage import Repository
from bcc.wizard import BotWizard
from bcc.wizard_presentation import grid_preview_data


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_native_qt_fixed_margin_create_edit_and_persist(app, tmp_path):
    cfg = defaults("VELES")
    cfg["name"] = "Native VELES fixed amount"
    cfg["coin_overrides"]["BTCUSDT"] = {
        "settings": {"allocation_mode": "fixed_usdt", "allocation_usdt": 42}
    }
    repo = Repository(tmp_path)
    created = []
    wizard = BotWizard(cfg, on_save=lambda value: created.append(repo.create(value)))
    try:
        mode = wizard.settings.finance.controls["allocation_mode"][1]
        assert mode.currentData() == "percent"
        assert effective(wizard.data(), "BTCUSDT")["allocation_usdt"] == 42
        wizard.commit()
        assert created
    finally:
        wizard.close()
    old, digest = repo.load(created[0])
    assert effective(old, "BTCUSDT")["allocation_usdt"] == 42
    reopen = BotWizard(old, on_save=lambda value: repo.save(created[0], value, digest))
    try:
        chooser = reopen.settings.finance.controls["allocation_mode"][1]
        chooser.setCurrentIndex(chooser.findData("fixed_usdt"))
        reopen.settings.finance.controls["allocation_usdt"][1].setValue(30)
        reopen.commit()
    finally:
        reopen.close()
    saved, _ = repo.load(created[0])
    assert saved["settings"]["allocation_mode"] == "fixed_usdt"
    assert saved["settings"]["allocation_usdt"] == 30
    assert effective(saved, "BTCUSDT")["allocation_usdt"] == 42
    assert effective(saved, "ETHUSDT")["allocation_usdt"] == 30
    sid = saved["sections"][0]["id"]
    assert "42 USDT" in grid_preview_data(saved, "BTCUSDT", sid)["status"]
    assert "30 USDT" in grid_preview_data(saved, "ETHUSDT", sid)["status"]


def test_native_qt_legacy_percent_reopen_is_non_destructive(app):
    cfg = defaults("VELES")
    cfg["name"] = "Legacy capital mode"
    del cfg["settings"]["allocation_mode"]
    del cfg["settings"]["allocation_usdt"]
    wizard = BotWizard(cfg)
    try:
        assert wizard.settings.finance.controls["allocation_mode"][1].currentData() == "percent"
        saved = wizard.data()
        assert saved["settings"]["allocation_mode"] == "percent"
        assert saved["settings"]["allocation_pct"] == 5
        assert saved["settings"]["allocation_usdt"] == 25
    finally:
        wizard.close()
