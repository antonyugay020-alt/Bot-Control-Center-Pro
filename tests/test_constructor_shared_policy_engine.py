"""One policy transaction for Qt coin, Qt market and batch edit paths.

The test runs with no Qt, exchange connection, worker or live PAPER state.
"""
from copy import deepcopy
import random

import pytest

from bcc.config import defaults, resolve_poly_market
from bcc.constructor_contract import (
    project_poly_policy_transaction, validate_coin_policy,
    validate_market_override, validate_constructor_roundtrip,
)
from bcc.storage import Repository


def config():
    cfg = defaults('POLYMARKET')
    cfg['name'] = 'Shared editor policy engine'
    cfg['symbols'] = ['BTC', 'ETH', 'SOL']
    cfg['polymarket'].update(durations=[300, 900, 3600],
        impulse_mode='usd', impulse=85, ask_min=.1, ask_max=.97,
        entry_signal_mode='before_close', entry_seconds_before_close=120,
        entry_window_seconds=25, entry_min_seconds_left=5)
    return cfg


@pytest.mark.parametrize('seed', range(48))
def test_coin_and_market_forms_match_atomic_batch_and_roundtrip(seed, tmp_path):
    rng = random.Random(20261105000 + seed)
    original = config()
    before = deepcopy(original)
    symbol = rng.choice(original['symbols'])
    duration = rng.choice(original['polymarket']['durations'])
    coin_rule = {'entry_seconds_before_close': rng.randint(50, 115),
                 'entry_window_seconds': rng.randint(1, 20),
                 'ask_min': round(rng.uniform(.15, .6), 4),
                 'disabled': bool(rng.getrandbits(1))}
    coin_patch = validate_coin_policy(original, symbol, coin_rule)
    staged = project_poly_policy_transaction(original, coin_rules={symbol: coin_rule})
    assert staged['coin_overrides'][symbol]['parameters'] == coin_patch
    # A market override can be edited in the presence of a coin override.
    market_rule = {'entry_seconds_before_close': rng.randint(25, 45),
                   'entry_window_seconds': rng.randint(1, 12),
                   'ask_min': round(rng.uniform(.62, .75), 4)}
    market_patch = validate_market_override(staged, symbol, duration, market_rule)
    key = f'{symbol}:{duration}'
    result = project_poly_policy_transaction(staged, market_rules={key: market_rule})
    assert result['polymarket']['overrides'][key] == market_patch
    assert resolve_poly_market(result, symbol, duration)['entry_seconds_before_close'] == market_rule['entry_seconds_before_close']
    assert original == before
    assert validate_constructor_roundtrip(result) == result
    repo = Repository(tmp_path)
    folder = repo.create(result)
    loaded, revision = repo.load(folder)
    assert loaded == result
    # Reopen, reset one market to inherited, and save again. Other markets
    # must not inherit the reset or change their signal timing.
    other = next(d for d in result['polymarket']['durations'] if d != duration)
    other_before = resolve_poly_market(result, symbol, other)
    assert validate_market_override(loaded, symbol, duration, {}) == {}
    reset = project_poly_policy_transaction(loaded, market_rules={key: {}})
    repo.save(folder, reset, revision)
    reopened, _ = repo.load(folder)
    assert key not in reopened['polymarket']['overrides']
    assert resolve_poly_market(reopened, symbol, other) == other_before


def test_combined_edit_is_valid_even_when_sequential_edits_are_not():
    cfg = config()
    cfg['coin_overrides']['ETH'] = {'parameters': {'entry_seconds_before_close': 60}}
    cfg['polymarket']['overrides']['ETH:300'] = {'entry_seconds_before_close': 30}
    untouched = deepcopy(cfg)
    # A new protective cutoff would invalidate the old market entry, but the
    # same atomic transaction also moves that market entry to a safe time.
    with pytest.raises(ValueError):
        validate_coin_policy(cfg, 'ETH', {'entry_min_seconds_left': 40})
    new = project_poly_policy_transaction(cfg,
        coin_rules={'ETH': {'entry_min_seconds_left': 40}},
        market_rules={'ETH:300': {'entry_seconds_before_close': 80}})
    assert resolve_poly_market(new, 'ETH', 300)['entry_seconds_before_close'] == 80
    assert resolve_poly_market(new, 'ETH', 300)['entry_min_seconds_left'] == 40
    assert cfg == untouched


@pytest.mark.parametrize('bad', [
    {'entry_seconds_before_close': 1},
    {'ask_min': .98},
    {'disabled': 'true'},
    {'entry_seconds_before_close': 3601},
    {'entry_window_seconds': 300},
])
def test_invalid_market_form_and_batch_both_rollback(bad):
    cfg = config()
    snapshot = deepcopy(cfg)
    with pytest.raises(ValueError):
        validate_market_override(cfg, 'BTC', 300, bad)
    with pytest.raises(ValueError):
        project_poly_policy_transaction(cfg, market_rules={'BTC:300': bad})
    assert cfg == snapshot


def test_effective_market_rule_excludes_entire_market_matrix():
    """Unrelated overrides must not be copied into each worker rule.

    The constructor may have many independent coin/duration combinations.
    Resolution must be proportional to one policy, not to the full matrix.
    """
    from bcc.config import poly_rule_keys
    cfg = config()
    cfg['symbols'] = [f'C{i}' for i in range(160)]
    cfg['polymarket']['overrides'] = {
        f'{symbol}:{duration}': {'ask_min': .2, 'impulse': 3.2}
        for symbol in cfg['symbols'] for duration in cfg['polymarket']['durations']
    }
    root_before = deepcopy(cfg)
    rule = resolve_poly_market(cfg, 'C0', 300)
    assert set(rule) == poly_rule_keys()
    assert 'durations' not in rule and 'overrides' not in rule
    assert rule['ask_min'] == .2
    for symbol in cfg['symbols']:
        for duration in cfg['polymarket']['durations']:
            resolved = resolve_poly_market(cfg, symbol, duration)
            assert set(resolved) == poly_rule_keys()
            assert resolved['impulse'] == 3.2
    assert cfg == root_before
    cfg['polymarket']['overrides']['C159:3600']['ask_min'] = .9
    assert resolve_poly_market(cfg, 'C0', 300) == rule
