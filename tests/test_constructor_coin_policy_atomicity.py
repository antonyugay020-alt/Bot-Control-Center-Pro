"""Coin editor validation is pure, multi-market, and repository-safe.

No Qt dependency, exchange requests, workers, or orders.
"""
from copy import deepcopy
import random

import pytest

from bcc.config import defaults, resolve_poly_market
from bcc.constructor_contract import validate_coin_policy, validate_constructor_roundtrip
from bcc.storage import Repository


def _base():
    cfg = defaults('POLYMARKET')
    cfg['name'] = 'Independent coin and market editor'
    cfg['symbols'] = ['BTC', 'ETH', 'SOL']
    cfg['polymarket'].update(
        durations=[300, 900, 3600], entry_signal_mode='before_close',
        entry_seconds_before_close=100, entry_window_seconds=20,
        entry_min_seconds_left=5, ask_min=.5, ask_max=.95,
    )
    return cfg


def test_coin_editor_validates_all_inherited_market_overrides_without_mutation():
    cfg = _base()
    cfg['polymarket']['overrides']['ETH:300'] = {
        'entry_seconds_before_close': 45, 'entry_window_seconds': 20,
    }
    cfg['polymarket']['overrides']['ETH:900'] = {'ask_min': .90}
    original = deepcopy(cfg)
    assert validate_coin_policy(cfg, 'ETH', {
        'entry_seconds_before_close': 80, 'disabled': True,
    }) == {'entry_seconds_before_close': 80, 'disabled': True}
    assert cfg == original
    # Inherited entry cutoff is now too late for the existing ETH:300 patch:
    # its 45-second entry is before the new 50-second protective cutoff.
    with pytest.raises(ValueError, match='Вход за N секунд'):
        validate_coin_policy(cfg, 'ETH', {
            'entry_min_seconds_left': 50,
        })
    assert cfg == original
    # An existing market ASK floor may also become invalid after changing
    # the coin-wide ASK ceiling; reject it before accepting the coin dialog.
    with pytest.raises(ValueError, match='ASK'):
        validate_coin_policy(cfg, 'ETH', {'ask_max': .85})
    assert cfg == original


def test_coin_policy_rejects_foreign_fields_and_unselected_coin():
    cfg = _base()
    for patch in ({'durations': [300]}, {'overrides': {}},
                  {'entry_seconds_before_clsoe': 60}):
        with pytest.raises(ValueError, match='Неизвестные'):
            validate_coin_policy(cfg, 'ETH', patch)
    with pytest.raises(ValueError, match='не выбрана'):
        validate_coin_policy(cfg, 'XRP', {'disabled': True})
    with pytest.raises(ValueError, match='объектом'):
        validate_coin_policy(cfg, 'ETH', None)
    with pytest.raises(ValueError, match='Polymarket'):
        validate_coin_policy(defaults('VELES'), 'BTCUSDT', {})


def test_coin_policy_reset_must_revalidate_dependent_market():
    cfg = _base()
    cfg['coin_overrides']['ETH'] = {'parameters': {
        'entry_seconds_before_close': 45, 'entry_window_seconds': 10,
    }}
    cfg['polymarket']['overrides']['ETH:300'] = {
        'entry_seconds_before_close': 40, 'entry_window_seconds': 8,
    }
    assert validate_coin_policy(cfg, 'ETH', {}) == {}
    cfg['polymarket']['overrides']['ETH:300'] = {'entry_window_seconds': 96}
    # Removing coin timing restores the global 100-second cutoff; an existing
    # 96-second market window would cross the 5-second protective cutoff.
    with pytest.raises(ValueError):
        validate_coin_policy(cfg, 'ETH', {})


@pytest.mark.parametrize('seed', range(24))
def test_randomized_coin_market_repository_roundtrip(seed, tmp_path):
    rng = random.Random(709_000 + seed)
    cfg = _base()
    cfg['name'] = f'Random multi-market {seed}'
    for symbol in cfg['symbols']:
        before = rng.randint(50, 240)
        window = rng.randint(1, min(30, before - 5))
        coin = {
            'entry_seconds_before_close': before,
            'entry_window_seconds': window,
            'disabled': bool(rng.getrandbits(1)),
            'impulse': round(rng.uniform(.01, 8), 5),
        }
        cfg['coin_overrides'][symbol] = {
            'parameters': validate_coin_policy(cfg, symbol, coin),
        }
    for symbol in cfg['symbols']:
        for duration in cfg['polymarket']['durations']:
            if rng.randrange(3) == 0:
                cfg['polymarket']['overrides'][f'{symbol}:{duration}'] = {
                    'ask_min': round(rng.uniform(.50, .80), 4),
                }
    # All coin changes are validated against the complete market matrix.
    for symbol in cfg['symbols']:
        patch = cfg['coin_overrides'][symbol]['parameters']
        assert validate_coin_policy(cfg, symbol, patch) == patch
    repo = Repository(tmp_path)
    folder = repo.create(cfg)
    restored, revision = repo.load(folder)
    assert restored == validate_constructor_roundtrip(cfg)
    target = rng.choice(cfg['symbols'])
    changed = deepcopy(restored)
    patch = changed['coin_overrides'][target]['parameters']
    patch['disabled'] = not patch.get('disabled', False)
    # Canonicalize: a value equal to the global policy is not an override.
    changed['coin_overrides'][target]['parameters'] = validate_coin_policy(
        changed, target, patch)
    repo.save(folder, changed, revision)
    reloaded, _ = repo.load(folder)
    assert reloaded == changed
    for symbol in cfg['symbols']:
        for duration in cfg['polymarket']['durations']:
            assert resolve_poly_market(reloaded, symbol, duration) == resolve_poly_market(changed, symbol, duration)
