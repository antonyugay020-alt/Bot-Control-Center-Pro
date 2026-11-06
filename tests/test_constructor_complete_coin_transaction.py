"""Full Qt coin-editor projection: no repository/worker/order side effects."""
from copy import deepcopy
import random
import pytest
from bcc.config import defaults, resolve_poly_market, effective
from bcc.constructor_contract import (project_constructor_coin_transaction,
                                      validate_constructor_roundtrip)
from bcc.storage import Repository


def poly():
    cfg = defaults('POLYMARKET')
    cfg['name'] = 'Independent PAPER coin rules'
    cfg['symbols'] = ['BTC', 'ETH', 'SOL']
    cfg['polymarket'].update(durations=[300, 900], impulse_mode='usd', impulse=85,
        entry_signal_mode='before_close', entry_seconds_before_close=90,
        entry_window_seconds=15, entry_min_seconds_left=5)
    return cfg


def test_full_coin_edit_persists_all_layers_and_is_independent(tmp_path):
    cfg = poly(); before = deepcopy(cfg)
    changes = {
        'ETH': {'settings': {'leverage': 4}, 'parameters': {
            'impulse': 4.2, 'entry_seconds_before_close': 40}},
        'SOL': {'parameters': {'impulse': 0.2, 'entry_seconds_before_close': 50}}
    }
    staged = project_constructor_coin_transaction(cfg, coin_rules=changes)
    assert cfg == before
    assert staged['coin_overrides'] == changes
    assert effective(staged, 'ETH')['leverage'] == 4
    assert resolve_poly_market(staged, 'ETH', 300)['entry_seconds_before_close'] == 40
    assert resolve_poly_market(staged, 'BTC', 300)['entry_seconds_before_close'] == 90
    assert validate_constructor_roundtrip(staged) == staged
    repo = Repository(tmp_path)
    folder = repo.create(staged)
    saved, digest = repo.load(folder)
    updated = project_constructor_coin_transaction(saved, coin_rules={'SOL': None})
    repo.save(folder, updated, digest)
    reopened, _ = repo.load(folder)
    assert 'SOL' not in reopened['coin_overrides']
    assert reopened['coin_overrides']['ETH'] == changes['ETH']


def test_invalid_risk_settings_block_coin_dialog_not_only_final_save():
    cfg = poly(); original = deepcopy(cfg)
    for bad in ({'settings': {'leverage': 200}},
                {'settings': {'orders': {'count': 0}}},
                {'sections': {'missing': {'enabled': False}}},
                {'parameters': {'entry_seconds_before_close': 2}},
                {'parameters': {'ask_min': 0.9, 'ask_max': 0.2}},
                {'parameters': {'disabled': 'yes'}},
                {'parameters': {'impulse_mode': 'invalid'}},
                {'parameters': {'entry_seconds_before_close': 10000}},
                {'parameters': {'unknown': 3}}):
        with pytest.raises(ValueError):
            project_constructor_coin_transaction(cfg, coin_rules={
                'BTC': {'parameters': {'impulse': 95}}, 'ETH': bad})
        assert cfg == original


def test_market_inheritance_validated_against_full_coin_patch():
    cfg = poly()
    cfg['polymarket']['overrides']['ETH:300'] = {
        'entry_seconds_before_close': 35, 'entry_window_seconds': 10}
    before = deepcopy(cfg)
    with pytest.raises(ValueError, match='ETH:300'):
        project_constructor_coin_transaction(cfg, coin_rules={
            'ETH': {'settings': {'leverage': 5},
                    'parameters': {'entry_min_seconds_left': 40}}})
    assert cfg == before


def test_incomplete_name_does_not_block_coin_edit():
    cfg = poly(); cfg['name'] = ''
    result = project_constructor_coin_transaction(cfg, coin_rules={
        'ETH': {'parameters': {'entry_seconds_before_close': 60}}})
    assert result['name'] == ''
    with pytest.raises(ValueError):
        validate_constructor_roundtrip(result)


@pytest.mark.parametrize('seed', range(30))
def test_randomized_multicoin_save_edit_load(seed, tmp_path):
    rng = random.Random(seed+810110)
    cfg = poly(); cfg['name'] += str(seed)
    updates = {}
    for sym in cfg['symbols']:
        updates[sym] = {'settings': {'leverage': rng.randint(1, 50)},
                        'parameters': {'entry_seconds_before_close': rng.randint(30, 85),
                                       'impulse': rng.randint(1, 200)}}
    staged = project_constructor_coin_transaction(cfg, coin_rules=updates)
    repo = Repository(tmp_path)
    folder = repo.create(staged)
    restored, revision = repo.load(folder)
    assert restored == staged
    target = rng.choice(cfg['symbols'])
    edited = deepcopy(updates[target]); edited['parameters']['entry_seconds_before_close'] = rng.randint(25, 85)
    new = project_constructor_coin_transaction(restored, coin_rules={target: edited})
    repo.save(folder, new, revision)
    final, _ = repo.load(folder)
    assert final == new
    for sym in cfg['symbols']:
        if sym != target:
            assert final['coin_overrides'][sym] == updates[sym]
