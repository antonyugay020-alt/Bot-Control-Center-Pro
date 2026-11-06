"""Review every effective constructor change without touching PAPER or workers."""
from copy import deepcopy
import random

import pytest

from bcc.config import defaults, resolve_poly_market
from bcc.constructor_review import (
    constructor_change_summary, constructor_change_review_text,
)
from bcc.constructor_contract import validate_constructor_roundtrip
from bcc.storage import Repository


def poly():
    c = defaults("POLYMARKET")
    c.update(name="Review Polymarket", symbols=["BTC", "ETH", "SOL"])
    c["polymarket"].update(
        durations=[300, 900], impulse_mode="usd", impulse=85,
        entry_signal_mode="before_close", entry_seconds_before_close=90,
        entry_window_seconds=10, entry_min_seconds_left=5,
    )
    return c


def veles():
    c = defaults("VELES")
    c.update(name="Review VELES", symbols=["BTCUSDT", "ETHUSDT"])
    return c


def test_poly_review_counts_inherited_cutoffs_and_explicit_market_exception(tmp_path):
    original = poly()
    original["polymarket"]["overrides"]["BTC:300"] = {
        "entry_seconds_before_close": 40}
    proposed = deepcopy(original)
    proposed["polymarket"]["entry_seconds_before_close"] = 70
    before = deepcopy(proposed)
    report = constructor_change_summary(original, proposed)
    assert report["combinations_modified"] == 5
    assert report["entry_seconds_changed"] == 5
    assert report["combinations_added"] == report["combinations_removed"] == 0
    assert "BTC:300" not in report["samples"]
    assert "Изменён вход за N секунд: 5" in constructor_change_review_text(original, proposed)
    assert original["polymarket"]["entry_seconds_before_close"] == 90
    assert proposed == before
    repo = Repository(tmp_path)
    folder = repo.create(validate_constructor_roundtrip(proposed))
    reopened, _ = repo.load(folder)
    assert resolve_poly_market(reopened, "BTC", 300)["entry_seconds_before_close"] == 40
    assert resolve_poly_market(reopened, "BTC", 900)["entry_seconds_before_close"] == 70
    assert constructor_change_summary(original, reopened) == report


def test_poly_review_separates_new_removed_markets_and_signal_filters():
    original = poly()
    proposed = deepcopy(original)
    proposed["symbols"] = ["BTC", "SOL", "DOGE"]
    proposed["polymarket"]["durations"] = [300, 3600]
    proposed["coin_overrides"]["BTC"] = {"parameters": {
        "entry_seconds_before_close": 60}}
    before = deepcopy(proposed)
    report = constructor_change_summary(original, proposed)
    assert report["combinations_added"] == 4  # 2 new 3600 markets + DOGE:300,3600
    assert report["combinations_removed"] == 4  # 3 old 900 markets + ETH:300
    assert report["combinations_modified"] == 1  # BTC:300 only
    assert report["entry_seconds_changed"] == 1
    assert report["coin_overrides_changed"] == 1
    assert proposed == before


def test_veles_review_reports_entry_exit_sections_and_risk_changes(tmp_path):
    original = veles()
    proposed = deepcopy(original)
    sid = original["sections"][0]["id"]
    proposed["coin_overrides"]["ETHUSDT"] = {
        "sections": {sid: {"entry": deepcopy(original["sections"][0]["entry"])}}}
    proposed["coin_overrides"]["ETHUSDT"]["sections"][sid]["entry"]["conditions"][0]["timeframe"] = 5
    proposed["settings"]["leverage"] = 4
    before = deepcopy(proposed)
    report = constructor_change_summary(original, proposed)
    assert report["combinations_modified"] == len(original["symbols"]) * len(original["sections"])  # shared risk
    assert report["signals_changed"] == 1
    assert report["coin_overrides_changed"] == 1
    assert proposed == before
    repo = Repository(tmp_path)
    folder = repo.create(validate_constructor_roundtrip(proposed))
    reopened, _ = repo.load(folder)
    assert constructor_change_summary(original, reopened) == report


@pytest.mark.parametrize("seed", range(40))
def test_randomized_poly_change_review_matches_effective_market_rules(seed):
    rng = random.Random(20261105_1414 + seed)
    original = poly()
    proposed = deepcopy(original)
    symbol = rng.choice(original["symbols"])
    duration = rng.choice(original["polymarket"]["durations"])
    before_seconds = resolve_poly_market(original, symbol, duration)["entry_seconds_before_close"]
    changed_seconds = rng.randint(25, 65)
    assert changed_seconds != before_seconds
    proposed["polymarket"]["overrides"][f"{symbol}:{duration}"] = {
        "entry_seconds_before_close": changed_seconds}
    before = deepcopy(proposed)
    report = constructor_change_summary(original, proposed)
    assert report["combinations_modified"] == 1
    assert report["entry_seconds_changed"] == 1
    assert report["samples"] == [f"{symbol}:{duration}"]
    assert original != proposed and proposed == before


def test_review_validates_input_and_sample_boundaries():
    c = poly()
    with pytest.raises(ValueError):
        constructor_change_summary(c, veles())
    for limit in (-1, 101, True):
        with pytest.raises(ValueError):
            constructor_change_summary(c, c, sample_limit=limit)
    assert constructor_change_summary(c, c, sample_limit=0)["samples"] == []
