"""New independent randomized constructor section transactions (no trading)."""
from copy import deepcopy
import random

import pytest

from bcc.config import defaults, range_section, coin_section, effective
from bcc.constructor_sections import project_constructor_sections_transaction as stage
from bcc.constructor_contract import validate_constructor_roundtrip
from bcc.storage import Repository


def sample(strategy='VELES'):
    c = defaults(strategy)
    c['name'] = f'{strategy} stage sections'
    c['symbols'] = ['BTC', 'ETH', 'SOL'] if strategy == 'POLYMARKET' else [
        'BTCUSDT', 'ETHUSDT', 'SOLUSDT']
    return c


def test_edit_renamed_range_and_per_coin_effective_settings_roundtrip(tmp_path):
    cfg = sample()
    cfg['coin_overrides']['ETHUSDT'] = {'settings': {'leverage': 7},
                                       'sections': {cfg['sections'][0]['id']: {'enabled': False}}}
    original = deepcopy(cfg)
    changed = deepcopy(cfg['sections'])
    changed[0]['label'] = 'New RSI long range'
    changed[0]['entry']['conditions'][0]['timeframe'] = 5
    changed[0]['exit']['conditions'][0]['timeframe'] = 1
    staged = stage(cfg, sections=changed)
    assert cfg == original
    assert staged['sections'] == changed
    assert coin_section(staged, 'ETHUSDT', changed[0])['enabled'] is False
    assert effective(staged, 'ETHUSDT', changed[0])['leverage'] == 7
    assert validate_constructor_roundtrip(staged) == staged
    repo = Repository(tmp_path)
    folder = repo.create(staged)
    restored, digest = repo.load(folder)
    assert restored == staged
    changed[0]['label'] = 'Second edit'
    updated = stage(restored, sections=changed)
    repo.save(folder, updated, digest)
    assert repo.load(folder)[0] == updated


def test_reject_inconsistent_parent_and_duplicated_range_without_mutation():
    cfg = sample()
    original = deepcopy(cfg)
    cases = []
    same = deepcopy(cfg['sections']); same[1]['id'] = same[0]['id']; cases.append(same)
    malformed = deepcopy(cfg['sections']); malformed[0]['entry']['conditions'][0]['timeframe'] = 7; cases.append(malformed)
    wrong_side = deepcopy(cfg['sections']); wrong_side[0]['side'] = 'BOTH'; cases.append(wrong_side)
    bad_parent = deepcopy(cfg['sections'])
    for section in bad_parent:
        section['range_parent_id'] = 'parent_1'
        section['range_parent_label'] = 'Same'
    bad_parent[1]['range_parent_label'] = 'Different'
    cases.append(bad_parent)
    cases.append([])
    for proposed in cases:
        before = deepcopy(proposed)
        with pytest.raises(ValueError):
            stage(cfg, sections=proposed)
        assert cfg == original
        assert proposed == before


def test_all_coin_inheritance_checked_before_any_change_is_accepted():
    cfg = sample()
    cfg['coin_overrides']['SOLUSDT'] = {'settings': {'leverage': 101}}
    original = deepcopy(cfg)
    proposal = deepcopy(cfg['sections'])
    proposal[0]['label'] = 'Edit should not bypass SOLUSDT risk'
    with pytest.raises(ValueError, match='SOLUSDT'):
        stage(cfg, sections=proposal)
    assert cfg == original


def test_polymarket_entry_seconds_and_market_exceptions_survive_section_projection(tmp_path):
    cfg = sample('POLYMARKET')
    cfg['polymarket'].update(entry_signal_mode='before_close',
                             entry_seconds_before_close=44,
                             entry_window_seconds=11,
                             entry_min_seconds_left=5)
    cfg['polymarket']['overrides']['BTC:300'] = {
        'entry_seconds_before_close': 60, 'entry_window_seconds': 10}
    changed = deepcopy(cfg['sections'])
    changed[0]['label'] = 'Non-trading editor metadata'
    staged = stage(cfg, sections=changed)
    assert staged['polymarket'] == cfg['polymarket']
    repo = Repository(tmp_path)
    folder = repo.create(staged)
    restored, _ = repo.load(folder)
    assert restored['polymarket'] == cfg['polymarket']


@pytest.mark.parametrize('seed', range(80))
def test_randomized_veles_ranges_and_coins_are_atomic(seed):
    rng = random.Random(31_000_001 + seed)
    cfg = sample()
    symbols = cfg['symbols']
    cfg['coin_overrides'] = {
        symbol: {'settings': {'leverage': rng.randint(1, 50)}}
        for symbol in symbols
    }
    original = deepcopy(cfg)
    proposal = deepcopy(cfg['sections'])
    for i in range(rng.randint(1, 5)):
        a = rng.randrange(1, 89)
        b = rng.randrange(a + 1, 100)
        section = range_section(a, b, 'LONG', rng.choice([1, 2, 5, 10, 15, 30]),
                                rng.choice([1, 2, 5, 10, 15, 30]))
        section['id'] = f'range_{seed}_{i}'
        section['label'] = f'Random {seed}/{i}'
        proposal.append(section)
    if seed % 9 == 0:
        proposal[-1]['entry']['conditions'][0]['value'] = 101
        with pytest.raises(ValueError):
            stage(cfg, sections=proposal)
    else:
        staged = stage(cfg, sections=proposal)
        assert staged['sections'] == proposal
        assert staged['coin_overrides'] == cfg['coin_overrides']
        assert validate_constructor_roundtrip(staged) == staged
    assert cfg == original
