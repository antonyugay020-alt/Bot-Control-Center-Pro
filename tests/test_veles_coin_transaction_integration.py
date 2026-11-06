"""Read-only VELES Qt coin-dialog validation and persisted create/edit/load."""
from copy import deepcopy
import random
import pytest

from bcc.config import defaults, coin_section, effective
from bcc.constructor_contract import (project_veles_coin_transaction,
                                      validate_constructor_roundtrip)
from bcc.storage import Repository


def base():
    cfg = defaults('VELES')
    cfg['name'] = 'VELES section and coin constructor'
    cfg['symbols'] = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']
    return cfg


def test_atomic_coin_section_edit_and_repository_roundtrip(tmp_path):
    cfg = base(); original = deepcopy(cfg)
    sid = cfg['sections'][0]['id']
    changes = {'ETHUSDT': {'sections': {sid: {'enabled': False}}},
               'SOLUSDT': {'settings': {'leverage': 4}}}
    staged = project_veles_coin_transaction(cfg, coin_rules=changes)
    assert cfg == original
    assert coin_section(staged, 'ETHUSDT', staged['sections'][0])['enabled'] is False
    assert effective(staged, 'SOLUSDT', staged['sections'][0])['leverage'] == 4
    assert validate_constructor_roundtrip(staged) == staged
    repo = Repository(tmp_path); folder = repo.create(staged)
    loaded, revision = repo.load(folder)
    assert loaded == staged
    changed = project_veles_coin_transaction(loaded, coin_rules={'ETHUSDT': None})
    repo.save(folder, changed, revision)
    reloaded, _ = repo.load(folder)
    assert 'ETHUSDT' not in reloaded['coin_overrides']
    assert reloaded['coin_overrides']['SOLUSDT'] == changes['SOLUSDT']


def test_invalid_section_and_risk_rejected_without_partial_coin_changes():
    cfg = base(); before = deepcopy(cfg)
    sid = cfg['sections'][0]['id']
    cases = [
        {'ETHUSDT': {'sections': {'missing_section': {'enabled': False}}}},
        {'ETHUSDT': {'sections': {sid: {'enabled': 'no'}}}},
        {'ETHUSDT': {'settings': {'leverage': 101}}},
        {'ETHUSDT': {'sections': {sid: {'entry': {'operator': 'BAD', 'conditions': []}}}}},
        {'ETHUSDT': {'sections': {sid: {'side': 'BOTH'}}}},
        {'ETHUSDT': {'settings': 'invalid'}},
        {'ETHUSDT': {'sections': {sid: {'settings': 'invalid'}}}},
    ]
    for invalid in cases:
        with pytest.raises(ValueError):
            project_veles_coin_transaction(cfg, coin_rules={
                'BTCUSDT': {'settings': {'leverage': 3}}, **invalid})
        assert cfg == before


def test_coin_dialog_accepts_unfinished_unrelated_bot_name():
    cfg = base(); cfg['name'] = ''
    sid = cfg['sections'][0]['id']
    staged = project_veles_coin_transaction(cfg, coin_rules={
        'BTCUSDT': {'sections': {sid: {'enabled': False}}}})
    assert staged['name'] == ''
    with pytest.raises(ValueError):
        validate_constructor_roundtrip(staged)


@pytest.mark.parametrize('seed', range(40))
def test_randomized_independent_coin_overrides(seed, tmp_path):
    rng = random.Random(991_000 + seed)
    cfg = base(); cfg['name'] += str(seed)
    sid = cfg['sections'][0]['id']
    updates = {}
    for symbol in cfg['symbols']:
        updates[symbol] = {'settings': {'leverage': rng.randint(1, 25)},
                           'sections': {sid: {'enabled': bool(rng.getrandbits(1))}}}
    staged = project_veles_coin_transaction(cfg, coin_rules=updates)
    repo = Repository(tmp_path); folder = repo.create(staged)
    restored, revision = repo.load(folder)
    assert restored == staged
    target = rng.choice(cfg['symbols'])
    updated = project_veles_coin_transaction(restored, coin_rules={
        target: {'settings': {'leverage': rng.randint(1, 25)},
                 'sections': {sid: {'enabled': False}}}})
    repo.save(folder, updated, revision)
    final, _ = repo.load(folder)
    assert final == updated
    for symbol in cfg['symbols']:
        if symbol != target:
            assert final['coin_overrides'][symbol] == updates[symbol]
