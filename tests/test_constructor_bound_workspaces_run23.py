"""Constructor edit provenance, read/write/commit and concurrent window isolation.

No Qt, worker, network, or trading account is used here.
"""
from copy import deepcopy
from hashlib import sha256
import json
import random

import pytest

from bcc.config import defaults, resolve_poly_market
from bcc.constructor_contract import validate_constructor_roundtrip
from bcc.constructor_edit_workspace import edit_workspace_binding, edit_workspace_path
from bcc.constructor_session import (
    load_constructor_draft_snapshot, save_constructor_draft_with_revision,
    remove_constructor_draft_if_revision,
)
from bcc.storage import Repository


def _config(strategy, seed):
    rng = random.Random(23000 + seed)
    cfg = defaults(strategy)
    cfg['name'] = f'Provenance {strategy} {seed}'
    if strategy == 'POLYMARKET':
        cfg['symbols'] = ['BTC', 'ETH', 'XRP']
        cfg['polymarket']['durations'] = [300, 900]
        cfg['polymarket']['entry_signal_mode'] = 'before_close'
        cfg['polymarket']['entry_seconds_before_close'] = 90
        cfg['polymarket']['entry_window_seconds'] = 0
        cfg['coin_overrides']['ETH'] = {'parameters': {
            'entry_seconds_before_close': rng.randint(50, 70)}}
        cfg['polymarket']['overrides']['ETH:900'] = {
            'entry_seconds_before_close': rng.randint(20, 45)}
    else:
        cfg['symbols'] = ['BTCUSDT', 'ETHUSDT']
        cfg['settings']['leverage'] = rng.randint(2, 12)
        cfg['sections'][0]['entry']['conditions'][0]['timeframe'] = 2
        cfg['sections'][0]['exit']['conditions'][0]['timeframe'] = 1
    return cfg


@pytest.mark.parametrize('strategy', ['VELES', 'POLYMARKET'])
@pytest.mark.parametrize('seed', range(45))
def test_bound_edit_roundtrip_publish_with_original_digest(tmp_path, strategy, seed):
    repo = Repository(tmp_path)
    owner = repo.create(_config(strategy, seed))
    original, digest = repo.load(owner)
    path = edit_workspace_path(repo.root, owner, digest)
    binding = edit_workspace_binding(repo.root, owner, digest)
    assert binding['source_digest'] == digest
    edited = deepcopy(original)
    edited['name'] += ' changed'
    if strategy == 'POLYMARKET':
        edited['polymarket']['overrides']['ETH:900']['entry_seconds_before_close'] = 55
    else:
        edited['settings']['leverage'] = 4
    _, revision = save_constructor_draft_with_revision(
        path, edited, expected_revision=None, binding=binding)
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document['format'] == 'bcc-constructor-edit-workspace-v1'
    assert document['binding'] == binding
    assert document['config'] == edited
    assert repo.load(owner) == (original, digest)
    with pytest.raises(ValueError, match='привязки|владельца'):
        load_constructor_draft_snapshot(path, expected_strategy=strategy)
    restored, same_revision = load_constructor_draft_snapshot(
        path, expected_strategy=strategy, binding=binding)
    assert (restored, same_revision) == (edited, revision)
    repo.save(owner, validate_constructor_roundtrip(restored), digest)
    published, new_digest = repo.load(owner)
    assert published == edited and new_digest != digest
    assert remove_constructor_draft_if_revision(path, revision)
    assert edit_workspace_path(repo.root, owner, new_digest) != path
    if strategy == 'POLYMARKET':
        assert resolve_poly_market(published, 'ETH', 900)['entry_seconds_before_close'] == 55
    else:
        assert published['sections'][0]['entry']['conditions'][0]['timeframe'] == 2
        assert published['sections'][0]['exit']['conditions'][0]['timeframe'] == 1


@pytest.mark.parametrize('strategy', ['VELES', 'POLYMARKET'])
def test_copied_draft_for_other_bot_or_revision_is_rejected_without_mutation(tmp_path, strategy):
    repo = Repository(tmp_path)
    a = repo.create(_config(strategy, 1))
    b = repo.create(_config(strategy, 2))
    a_cfg, a_digest = repo.load(a)
    b_cfg, b_digest = repo.load(b)
    a_path = edit_workspace_path(repo.root, a, a_digest)
    b_path = edit_workspace_path(repo.root, b, b_digest)
    a_binding = edit_workspace_binding(repo.root, a, a_digest)
    b_binding = edit_workspace_binding(repo.root, b, b_digest)
    save_constructor_draft_with_revision(a_path, a_cfg, expected_revision=None, binding=a_binding)
    b_path.write_bytes(a_path.read_bytes())
    before = b_path.read_bytes()
    with pytest.raises(ValueError, match='другому боту'):
        load_constructor_draft_snapshot(b_path, binding=b_binding, allow_legacy=True)
    with pytest.raises(ValueError, match='другому боту'):
        save_constructor_draft_with_revision(
            b_path, b_cfg, expected_revision=sha256(before).hexdigest(),
            binding=b_binding, allow_legacy=True)
    assert b_path.read_bytes() == before
    assert repo.load(a) == (a_cfg, a_digest)
    assert repo.load(b) == (b_cfg, b_digest)


@pytest.mark.parametrize('strategy', ['VELES', 'POLYMARKET'])
def test_legacy_unbound_edit_draft_migrates_only_after_explicit_opt_in(tmp_path, strategy):
    repo = Repository(tmp_path)
    owner = repo.create(_config(strategy, 3))
    cfg, digest = repo.load(owner)
    path = edit_workspace_path(repo.root, owner, digest)
    binding = edit_workspace_binding(repo.root, owner, digest)
    _, revision = save_constructor_draft_with_revision(path, cfg, expected_revision=None)
    before = path.read_bytes()
    with pytest.raises(ValueError, match='привязку'):
        load_constructor_draft_snapshot(path, binding=binding)
    with pytest.raises(ValueError, match='привязку'):
        save_constructor_draft_with_revision(path, cfg, expected_revision=revision, binding=binding)
    assert path.read_bytes() == before
    resumed, rev = load_constructor_draft_snapshot(
        path, binding=binding, expected_strategy=strategy, allow_legacy=True)
    assert (resumed, rev) == (cfg, revision)
    _, new_revision = save_constructor_draft_with_revision(
        path, resumed, expected_revision=rev, binding=binding, allow_legacy=True)
    assert new_revision != revision
    assert load_constructor_draft_snapshot(path, binding=binding)[0] == cfg
    assert repo.load(owner) == (cfg, digest)


def test_malformed_owner_binding_fails_before_file_write(tmp_path):
    cfg = _config('VELES', 4)
    for owner in ('MY_BOTS/../outside', 'MY_BOTS/a/./b', 'MY_BOTS/a\\b'):
        path = tmp_path / 'draft.json'
        with pytest.raises(ValueError, match='привязка'):
            save_constructor_draft_with_revision(
                path, cfg, expected_revision=None,
                binding={'owner': owner, 'source_digest': 'a' * 64})
        assert not path.exists()
