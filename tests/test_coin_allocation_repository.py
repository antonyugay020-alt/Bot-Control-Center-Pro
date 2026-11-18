"""VELES coin allocation persists through the existing atomic repository path.

No real orders, API keys, network requests, account processes or shared
trading databases. All repositories are temporary test directories.
"""
from copy import deepcopy

import pytest

from bcc.config import defaults, effective, validate
from bcc.constructor_contract import validate_constructor_roundtrip
from bcc.storage import Repository, Store, initial_state
from bcc.strategies import capital_margin


def configured():
    cfg = defaults("VELES")
    cfg["name"] = "isolated coin allocation"
    cfg["symbols"] = ["BTCUSDT", "ETHUSDT"]
    cfg["settings"].update(
        allocation_mode="coin_pct", allocation_pct=5,
        allocation_fixed_usdt=25,
    )
    cfg["coin_overrides"]["BTCUSDT"] = {
        "settings": {"allocation_mode": "coin_fixed",
                     "allocation_fixed_usdt": 40}}
    return cfg


def test_repository_create_load_edit_preserves_both_coin_policies(tmp_path):
    repo = Repository(tmp_path)
    start = configured()
    folder = repo.create(start)
    loaded, revision = repo.load(folder)
    assert loaded == validate_constructor_roundtrip(start)
    assert loaded["settings"]["allocation_mode"] == "coin_pct"
    assert effective(loaded, "BTCUSDT", loaded["sections"][0])[
        "allocation_fixed_usdt"] == 40
    assert effective(loaded, "ETHUSDT", loaded["sections"][0])[
        "allocation_mode"] == "coin_pct"

    proposed = deepcopy(loaded)
    proposed["coin_overrides"]["BTCUSDT"]["settings"]["allocation_fixed_usdt"] = 55
    repo.save(folder, proposed, revision)
    after, _ = repo.load(folder)
    assert after["coin_overrides"]["BTCUSDT"]["settings"][
        "allocation_fixed_usdt"] == 55
    assert capital_margin(after, initial_state(after["capital"]),
                          effective(after, "BTCUSDT", after["sections"][0]),
                          symbol="BTCUSDT") == pytest.approx(55)
    assert capital_margin(after, initial_state(after["capital"]),
                          effective(after, "ETHUSDT", after["sections"][0]),
                          symbol="ETHUSDT") == pytest.approx(25)


def test_invalid_mixed_section_budget_is_rejected_before_repository_mutation(tmp_path):
    repo = Repository(tmp_path)
    folder = repo.create(configured())
    loaded, revision = repo.load(folder)
    invalid = deepcopy(loaded)
    invalid["sections"][1]["overrides"]["allocation_fixed_usdt"] = 99
    # This is invalid on BTC's inherited fixed policy.
    with pytest.raises(ValueError, match="общая квота"):
        repo.save(folder, invalid, revision)
    after, unchanged_revision = repo.load(folder)
    assert after == loaded
    assert unchanged_revision == revision


def test_existing_legacy_bot_settings_migrate_without_fixed_mode(tmp_path):
    repo = Repository(tmp_path)
    old = defaults("VELES")
    old["name"] = "legacy percentage"
    old["settings"].pop("allocation_mode")
    old["settings"].pop("allocation_fixed_usdt")
    folder = repo.create(old)
    loaded, _ = repo.load(folder)
    assert loaded["settings"]["allocation_mode"] == "section_pct"
    assert loaded["settings"]["allocation_pct"] == 5.0
    assert loaded["settings"]["allocation_fixed_usdt"] == 25.0
    for symbol in loaded["symbols"]:
        for section in loaded["sections"]:
            assert effective(loaded, symbol, section)["allocation_mode"] == "section_pct"


def test_fixed_coin_budget_preserves_paper_account_when_changing_bot_settings(tmp_path):
    repo = Repository(tmp_path)
    original = configured()
    folder = repo.create(original)
    Store(folder, original["capital"])
    before_account = Store(folder, original["capital"]).read(include_charts=False)
    before_history = list(Store(folder).history())
    loaded, rev = repo.load(folder)
    draft = deepcopy(loaded)
    draft["coin_overrides"]["BTCUSDT"]["settings"]["allocation_fixed_usdt"] = 33
    repo.save(folder, draft, rev)
    after_account = Store(folder, original["capital"]).read(include_charts=False)
    assert after_account["cash"] == pytest.approx(before_account["cash"])
    assert after_account["positions"] == before_account["positions"]
    assert after_account["pending"] == before_account["pending"]
    assert list(Store(folder).history()) == before_history
    Store(folder).audit()
