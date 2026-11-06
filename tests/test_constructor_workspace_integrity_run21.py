"""Cross-strategy workspace integrity and non-trading save/load/edit E2E."""
from copy import deepcopy
from hashlib import sha256
import json
import random

import pytest

from bcc.config import defaults, resolve_poly_market
from bcc.constructor_session import (
    load_constructor_draft_snapshot, save_constructor_draft,
    save_constructor_draft_with_revision,
)
from bcc.constructor_contract import validate_constructor_roundtrip
from bcc.storage import Repository
from bcc.wizard_state import selected_configuration


def candidate(strategy, seed):
    rng = random.Random(210000 + seed)
    cfg = defaults(strategy)
    cfg['name'] = f'Constructor lifecycle {seed}'
    if strategy == 'POLYMARKET':
        cfg['symbols'] = ['BTC', 'ETH']
        cfg['polymarket']['entry_signal_mode'] = 'before_close'
        cfg['polymarket']['durations'] = [300, 900]
        cfg['polymarket']['entry_seconds_before_close'] = 80
        cfg['polymarket']['entry_window_seconds'] = 20
        cfg['coin_overrides']['ETH'] = {'parameters': {'entry_seconds_before_close': 70}}
        cfg['polymarket']['overrides']['ETH:900'] = {
            'entry_seconds_before_close': rng.randint(25, 60),
            'entry_window_seconds': rng.randint(2, 8),
        }
    else:
        cfg['symbols'] = ['BTCUSDT', 'ETHUSDT']
        cfg['settings']['leverage'] = rng.randint(2, 8)
        cfg['coin_overrides']['ETHUSDT'] = {'settings': {'leverage': rng.randint(2, 8)}}
        cfg['sections'][0]['entry']['conditions'][0]['timeframe'] = rng.choice([1, 2, 5, 10, 15, 30])
    return cfg


@pytest.mark.parametrize('strategy', ['VELES', 'POLYMARKET'])
@pytest.mark.parametrize('seed', range(40))
def test_create_edit_reload_with_dormant_workspace_and_no_worker(tmp_path, strategy, seed):
    repo = Repository(tmp_path)
    original = candidate(strategy, seed)
    workspace = deepcopy(original)
    dormant = workspace['symbols'].pop()
    path = tmp_path / 'workspace.json'
    stored, revision = save_constructor_draft_with_revision(path, workspace, expected_revision=None)
    assert stored == workspace
    restored, revision2 = load_constructor_draft_snapshot(path, expected_strategy=strategy)
    assert revision == revision2
    selected, dropped = selected_configuration(restored)
    assert dropped and dormant not in selected['coin_overrides']
    if strategy == 'POLYMARKET':
        assert 'ETH:900' not in selected['polymarket']['overrides']
    folder = repo.create(validate_constructor_roundtrip(selected))
    disk, digest = repo.load(folder)
    assert disk == selected
    assert not (folder / 'state.sqlite3').exists()
    # Edit the *saved bot* using the same persistence path. The workspace is
    # separate and still retains the temporarily deselected rules.
    changed = deepcopy(disk)
    changed['name'] += ' edited'
    if strategy == 'POLYMARKET':
        changed['polymarket']['entry_seconds_before_close'] = 75
    else:
        changed['settings']['leverage'] = 4
    repo.save(folder, changed, digest)
    loaded, _ = repo.load(folder)
    assert loaded == validate_constructor_roundtrip(changed)
    assert not (folder / 'state.sqlite3').exists()
    resumed, _ = load_constructor_draft_snapshot(path, expected_strategy=strategy)
    resumed['symbols'].append(dormant)
    if strategy == 'POLYMARKET':
        assert resolve_poly_market(resumed, 'ETH', 900)['entry_seconds_before_close'] == original['polymarket']['overrides']['ETH:900']['entry_seconds_before_close']
    else:
        assert resumed['coin_overrides'][dormant] == original['coin_overrides'][dormant]


@pytest.mark.parametrize('strategy', ['VELES', 'POLYMARKET'])
def test_unconditional_legacy_save_does_not_overwrite_other_strategy_or_corruption(tmp_path, strategy):
    path = tmp_path / 'draft.json'
    original = candidate(strategy, 1)
    save_constructor_draft(path, original)
    before = path.read_bytes()
    other = 'POLYMARKET' if strategy == 'VELES' else 'VELES'
    with pytest.raises(ValueError, match='другой стратегии'):
        save_constructor_draft(path, candidate(other, 2))
    assert path.read_bytes() == before
    path.write_bytes(b'{"broken":')
    damaged = path.read_bytes()
    with pytest.raises((ValueError, json.JSONDecodeError)):
        save_constructor_draft(path, original)
    assert path.read_bytes() == damaged
    # A new workspace can be created without touching the damaged file.
    second = tmp_path / 'new_draft.json'
    assert save_constructor_draft(second, original) == original
    assert sha256(path.read_bytes()).digest() == sha256(damaged).digest()


@pytest.mark.parametrize('strategy', ['VELES', 'POLYMARKET'])
@pytest.mark.parametrize('field', ['coin_patch', 'section_patch', 'section_id', 'market_patch', 'duration'])
def test_structurally_corrupt_draft_rejected_before_atomic_write(tmp_path, strategy, field):
    cfg = candidate(strategy, 3)
    original = deepcopy(cfg)
    path = tmp_path / 'draft.json'
    save_constructor_draft(path, cfg)
    before = path.read_bytes()
    if field == 'coin_patch':
        cfg['coin_overrides'][cfg['symbols'][1]] = []
    elif field == 'section_patch':
        cfg['coin_overrides'][cfg['symbols'][1]] = {'sections': {'bad': 123}}
    elif field == 'section_id':
        cfg['sections'][0]['id'] = None
    elif field == 'market_patch':
        if strategy != 'POLYMARKET':
            return
        cfg['polymarket']['overrides']['BTC:300'] = []
    elif field == 'duration':
        if strategy != 'POLYMARKET':
            return
        cfg['polymarket']['durations'] = [300, '900']
    with pytest.raises((ValueError, TypeError)):
        save_constructor_draft(path, cfg)
    assert path.read_bytes() == before
    assert load_constructor_draft_snapshot(path)[0] == original
