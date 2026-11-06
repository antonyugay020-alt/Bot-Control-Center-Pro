"""Pure preview contract: selection is presentation, never a publishable rule."""
from copy import deepcopy

from bcc.config import defaults
from bcc.constructor_review import constructor_review_text
from bcc.wizard_presentation import grid_preview_data


def _multi_coin_draft():
    cfg = defaults("VELES")
    cfg["symbols"] = ["BTCUSDT", "ETHUSDT"]
    cfg["settings"]["orders"].update(count=2, coverage_pct=15, martingale_pct=0)
    cfg["settings"]["stop"].update(mode="both", pct=5)
    first_section = cfg["sections"][0]
    cfg["coin_overrides"] = {
        "BTCUSDT": {
            "sections": {
                first_section["id"]: {"side": "SHORT", "settings": {"leverage": 4}}
            }
        }
    }
    return cfg, first_section["id"]


def test_grid_preview_is_pure_and_resolves_per_coin_side_margin_stop():
    cfg, section_id = _multi_coin_draft()
    snapshot = deepcopy(cfg)
    btc = grid_preview_data(cfg, "BTCUSDT", section_id)
    eth = grid_preview_data(cfg, "ETHUSDT", section_id)
    assert cfg == snapshot
    assert btc["rows"][1][1] == "115"
    assert float(btc["rows"][0][3]) == 50
    assert "SHORT" in btc["status"]
    assert "Условный SL: 120" in btc["status"]
    assert eth["rows"][1][1] == "85"
    assert float(eth["rows"][0][3]) == 37.5
    assert "LONG" in eth["status"]
    assert "Условный SL: 80" in eth["status"]


def test_invalid_or_deleted_selection_cannot_fabricate_grid():
    cfg, section_id = _multi_coin_draft()
    assert grid_preview_data(cfg, "DELETEDUSDT", section_id) is None
    assert grid_preview_data(cfg, "BTCUSDT", "deleted-section") is None
    assert grid_preview_data(cfg, None, section_id) is None


def test_interactive_preview_selection_cannot_change_constructor_audit():
    cfg, section_id = _multi_coin_draft()
    original = deepcopy(cfg)
    original["capital"] -= 100
    expected = constructor_review_text(original, cfg, include_change_audit=True)
    assert "Изменения относительно" in expected
    for coin in ("BTCUSDT", "ETHUSDT", "BTCUSDT"):
        assert grid_preview_data(cfg, coin, section_id) is not None
        assert constructor_review_text(original, cfg, include_change_audit=True) == expected
