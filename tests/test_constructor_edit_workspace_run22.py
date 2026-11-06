"""Non-trading edit/reopen/commit provenance tests for VELES and Polymarket."""
from copy import deepcopy
from hashlib import sha256
import random

import pytest

from bcc.config import defaults, resolve_poly_market
from bcc.constructor_edit_workspace import edit_workspace_path
from bcc.constructor_session import (
    load_constructor_draft_snapshot, save_constructor_draft_with_revision,
    remove_constructor_draft_if_revision,
)
from bcc.constructor_contract import validate_constructor_roundtrip
from bcc.storage import Repository


def original(strategy, seed):
    rng = random.Random(220000 + seed)
    cfg = defaults(strategy)
    cfg['name'] = f'Existing {strategy} {seed}'
    cfg['symbols'] = ['BTC', 'ETH'] if strategy == 'POLYMARKET' else ['BTCUSDT', 'ETHUSDT']
    if strategy == 'POLYMARKET':
        cfg['polymarket']['durations'] = [300, 900]
        cfg['polymarket']['entry_signal_mode'] = 'before_close'
        cfg['polymarket']['entry_seconds_before_close'] = rng.randint(75, 100)
        cfg['polymarket']['entry_window_seconds'] = 0
        cfg['polymarket']['overrides']['ETH:900'] = {'entry_seconds_before_close': rng.randint(20, 50)}
    else:
        cfg['settings']['leverage'] = rng.randint(2, 10)
        cfg['coin_overrides']['ETHUSDT'] = {'settings': {'leverage': rng.randint(2, 10)}}
    return cfg


@pytest.mark.parametrize('strategy', ['VELES', 'POLYMARKET'])
@pytest.mark.parametrize('seed', range(45))
def test_version_bound_edit_draft_can_resume_and_publish_only_via_repository(tmp_path, strategy, seed):
    repo = Repository(tmp_path)
    owner = repo.create(original(strategy, seed))
    cfg, digest = repo.load(owner)
    path = edit_workspace_path(repo.root, owner, digest)
    edited = deepcopy(cfg)
    edited['name'] += ' changed'
    if strategy == 'POLYMARKET':
        edited['polymarket']['overrides']['ETH:900']['entry_seconds_before_close'] = 55
    else:
        edited['settings']['leverage'] = 11
    _, revision = save_constructor_draft_with_revision(path, edited, expected_revision=None)
    assert repo.load(owner) == (cfg, digest)
    resumed, same_revision = load_constructor_draft_snapshot(path, expected_strategy=strategy)
    assert same_revision == revision and resumed == edited
    assert validate_constructor_roundtrip(resumed) == resumed
    repo.save(owner, resumed, digest)
    published, newer_digest = repo.load(owner)
    assert published == edited and newer_digest != digest
    assert edit_workspace_path(repo.root, owner, newer_digest) != path
    assert remove_constructor_draft_if_revision(path, revision)
    assert not path.exists()
    assert not (owner / 'paper.sqlite').exists()
    if strategy == 'POLYMARKET':
        assert resolve_poly_market(published, 'ETH', 900)['entry_seconds_before_close'] == 55


def test_two_bots_and_two_versions_never_share_edit_draft(tmp_path):
    repo = Repository(tmp_path)
    a = repo.create(original('VELES', 1))
    b = repo.create(original('VELES', 2))
    a_cfg, a_digest = repo.load(a)
    _, b_digest = repo.load(b)
    a_path = edit_workspace_path(repo.root, a, a_digest)
    b_path = edit_workspace_path(repo.root, b, b_digest)
    assert a_path != b_path
    save_constructor_draft_with_revision(a_path, a_cfg, expected_revision=None)
    updated = deepcopy(a_cfg)
    updated['name'] = 'different revision'
    repo.save(a, updated, a_digest)
    _, new_digest = repo.load(a)
    assert edit_workspace_path(repo.root, a, new_digest) not in (a_path, b_path)
    assert a_path.exists()
    assert not b_path.exists()


def test_edit_draft_path_rejects_foreign_owner_and_invalid_revision(tmp_path):
    repo = Repository(tmp_path)
    owner = repo.create(original('POLYMARKET', 1))
    _, digest = repo.load(owner)
    with pytest.raises(ValueError):
        edit_workspace_path(repo.root, tmp_path, digest)
    with pytest.raises(ValueError):
        edit_workspace_path(repo.root, owner, '0' * 63)
    assert edit_workspace_path(repo.root, owner, digest).parent == repo.root
