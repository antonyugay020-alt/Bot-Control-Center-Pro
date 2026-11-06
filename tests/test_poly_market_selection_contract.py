"""Full market edit access independent of the bounded Modern Qt preview."""
from copy import deepcopy
import random

import pytest

from bcc.config import defaults
from bcc.market_selection import market_axes, market_key, market_preview


def poly(symbols, durations):
    cfg = defaults("POLYMARKET")
    cfg["symbols"] = symbols
    cfg["polymarket"]["durations"] = durations
    return cfg


def test_large_matrix_has_bounded_preview_but_all_markets_are_editable():
    cfg = poly([f"COIN{i}" for i in range(5000)], [300, 900, 3600])
    rows, total = market_preview(cfg)
    assert total == 15000
    assert len(rows) == 300
    assert rows[0] == ("COIN0", 300)
    assert market_key(cfg, "COIN4999", 3600) == "COIN4999:3600"
    assert market_axes(cfg)[0] is cfg["symbols"]


def test_small_preview_and_empty_preview_are_stable():
    cfg = poly(["BTC", "ETH"], [300, 900])
    assert market_preview(cfg) == ([('BTC', 300), ('BTC', 900), ('ETH', 300), ('ETH', 900)], 4)
    assert market_preview(cfg, 0) == ([], 4)
    before = deepcopy(cfg)
    for limit in (-1, True, 1.0):
        with pytest.raises(ValueError):
            market_preview(cfg, limit)
    assert cfg == before


def test_invalid_market_rejected_without_mutating_draft():
    cfg = poly(["BTC", "ETH"], [300, 900])
    original = deepcopy(cfg)
    for symbol, duration in [("SOL", 300), ("BTC", 3600), ("BTC", "300"), ("ETH", True)]:
        with pytest.raises(ValueError):
            market_key(cfg, symbol, duration)
    assert cfg == original


@pytest.mark.parametrize("seed", range(36))
def test_randomized_complete_selection_without_cartesian_list(seed):
    rng = random.Random(20261105_1500 + seed)
    count = rng.randint(301, 2500)
    symbols = [f"TOKEN{i}" for i in range(count)]
    durations = rng.sample([60, 120, 300, 600, 900, 1800, 3600], k=rng.randint(1, 7))
    cfg = poly(symbols, durations)
    preview, total = market_preview(cfg)
    assert len(preview) == min(300, total)
    assert total == len(symbols) * len(durations)
    assert market_key(cfg, symbols[-1], durations[-1]) == f"{symbols[-1]}:{durations[-1]}"
    assert market_key(cfg, symbols[0], durations[0]) == f"{symbols[0]}:{durations[0]}"


def test_market_outside_preview_roundtrips_timing_and_ask_through_repository(tmp_path):
    from bcc.config import resolve_poly_market
    from bcc.constructor_contract import validate_market_override, validate_constructor_roundtrip
    from bcc.storage import Repository

    cfg = poly([f"T{i:03d}" for i in range(320)], [300])
    cfg['name'] = 'Full matrix selection persistence'
    cfg['polymarket'].update(
        entry_signal_mode='before_close', entry_seconds_before_close=90,
        entry_window_seconds=10, entry_min_seconds_left=5)
    rows, total = market_preview(cfg)
    assert total == 320 and ('T319', 300) not in rows
    selected = market_key(cfg, 'T319', 300)
    before = deepcopy(cfg)
    rule = resolve_poly_market(cfg, 'T319', 300)
    proposal = dict(rule, entry_seconds_before_close=45, ask_min=.35)
    patch = validate_market_override(cfg, 'T319', 300, proposal)
    assert cfg == before
    cfg['polymarket']['overrides'][selected] = patch
    repo = Repository(tmp_path)
    folder = repo.create(validate_constructor_roundtrip(cfg))
    reopened, digest = repo.load(folder)
    assert resolve_poly_market(reopened, 'T319', 300)['entry_seconds_before_close'] == 45
    assert resolve_poly_market(reopened, 'T319', 300)['ask_min'] == .35
    assert resolve_poly_market(reopened, 'T318', 300)['entry_seconds_before_close'] == 90
    # Reopen, reset only the selected market, and save without changing any
    # unrelated account or live bot process.
    reopened['polymarket']['overrides'].pop(selected)
    repo.save(folder, validate_constructor_roundtrip(reopened), digest)
    final, _ = repo.load(folder)
    assert selected not in final['polymarket']['overrides']
    assert resolve_poly_market(final, 'T319', 300)['entry_seconds_before_close'] == 90
