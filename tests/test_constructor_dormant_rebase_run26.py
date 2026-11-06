"""Regression: edit/save must not orphan disabled policies when digest changes.

All tests use temporary PAPER repositories. No workers, network or orders.
"""
from copy import deepcopy
from pathlib import Path
import random

import pytest

from bcc.config import defaults, resolve_poly_market, range_section
from bcc.constructor_edit_workspace import (
    edit_workspace_binding, edit_workspace_path, rebase_dormant_workspace,
    retain_workspace_after_commit,
)
from bcc.constructor_session import (
    load_constructor_draft_snapshot, save_constructor_draft_with_revision,
)
from bcc.constructor_transaction import validate_constructor_publish
from bcc.storage import Repository


def sample(strategy, seed):
    rng = random.Random(20261106026 + seed)
    c = defaults(strategy)
    c['name'] = f'Run26 dormant workspace {seed}'
    if strategy == 'POLYMARKET':
        c['symbols'] = ['BTC', 'ETH', 'SOL']
        c['polymarket']['entry_signal_mode'] = 'before_close'
        c['polymarket']['entry_seconds_before_close'] = 90
        c['polymarket']['entry_window_seconds'] = 15
        c['polymarket']['overrides']['ETH:900'] = {
            'entry_seconds_before_close': 50,
            'entry_window_seconds': 8,
        }
        c['polymarket']['overrides']['SOL:3600'] = {
            'entry_seconds_before_close': 240,
            'entry_window_seconds': 30,
        }
        c['coin_overrides']['SOL'] = {
            'parameters': {'stake_pct': rng.randint(2, 12)}}
    else:
        c['symbols'] = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']
        c['coin_overrides']['SOLUSDT'] = {
            'settings': {'leverage': rng.randint(2, 12)}}
        sid = c['sections'][1]['id']
        c['coin_overrides']['BTCUSDT'] = {
            'sections': {sid: {'settings': {'leverage': rng.randint(2, 12)}}}}
    return c


@pytest.mark.parametrize('strategy', ['VELES', 'POLYMARKET'])
@pytest.mark.parametrize('seed', range(30))
def test_rebase_survives_digest_change_without_live_policy_drift(tmp_path, strategy, seed):
    repo = Repository(tmp_path)
    initial = sample(strategy, seed)
    folder = repo.create(validate_constructor_publish(initial))
    live, old_digest = repo.load(folder)
    workspace = deepcopy(live)
    inactive = workspace['symbols'].pop()
    if strategy == 'POLYMARKET':
        workspace['polymarket']['durations'].remove(900)
        workspace['polymarket']['entry_seconds_before_close'] = 70
        workspace['polymarket']['overrides']['BTC:900'] = {
            'entry_seconds_before_close': 42, 'entry_window_seconds': 10}
    else:
        inactive_sid = workspace['sections'].pop()['id']
        workspace['settings']['leverage'] = 4
    proposed = validate_constructor_publish(workspace)
    assert inactive not in proposed['coin_overrides']
    repo.save(folder, proposed, old_digest)
    saved, new_digest = repo.load(folder)
    assert saved == proposed and new_digest != old_digest
    assert not (folder / 'state.sqlite3').exists()
    carried = rebase_dormant_workspace(workspace, saved)
    assert validate_constructor_publish(carried) == saved
    assert inactive in carried['coin_overrides']
    if strategy == 'POLYMARKET':
        assert 'ETH:900' in carried['polymarket']['overrides']
        assert 'BTC:900' in carried['polymarket']['overrides']
        assert 'SOL:3600' in carried['polymarket']['overrides']
        assert carried['polymarket']['entry_seconds_before_close'] == 70
    else:
        assert inactive_sid in carried['coin_overrides']['BTCUSDT']['sections']
        assert carried['settings']['leverage'] == 4
    new_path = edit_workspace_path(repo.root, folder, new_digest)
    binding = edit_workspace_binding(repo.root, folder, new_digest)
    _, rev = save_constructor_draft_with_revision(
        new_path, carried, expected_revision=None, binding=binding)
    reopened, loaded_rev = load_constructor_draft_snapshot(
        new_path, expected_strategy=strategy, binding=binding)
    assert loaded_rev == rev and reopened == carried
    assert validate_constructor_publish(reopened) == saved
    assert new_path != edit_workspace_path(repo.root, folder, old_digest)
    if strategy == 'POLYMARKET':
        reopened['symbols'].append(inactive)
        reopened['polymarket']['durations'].append(900)
        assert resolve_poly_market(reopened, 'ETH', 900)['entry_seconds_before_close'] == 50
    else:
        assert reopened['coin_overrides'][inactive] == initial['coin_overrides'][inactive]


def test_rebase_rejects_mismatched_publication(tmp_path):
    workspace = sample('POLYMARKET', 1)
    published = validate_constructor_publish(workspace)
    published['polymarket']['entry_seconds_before_close'] = 120
    with pytest.raises(ValueError, match='не соответствует'):
        rebase_dormant_workspace(workspace, published)


def test_new_digest_draft_collision_never_overwrites(tmp_path):
    repo = Repository(tmp_path)
    config = sample('POLYMARKET', 4)
    folder = repo.create(validate_constructor_publish(config))
    _, digest = repo.load(folder)
    path = edit_workspace_path(repo.root, folder, digest)
    binding = edit_workspace_binding(repo.root, folder, digest)
    original, revision = save_constructor_draft_with_revision(
        path, config, expected_revision=None, binding=binding)
    before = path.read_bytes()
    with pytest.raises(ValueError, match='другом окне'):
        save_constructor_draft_with_revision(
            path, config, expected_revision=None, binding=binding)
    assert path.read_bytes() == before
    restored, current = load_constructor_draft_snapshot(path, binding=binding)
    assert restored == original and current == revision


def test_qt_commit_and_edit_wiring_preserves_workspace_after_publication():
    root = Path(__file__).resolve().parents[1] / 'bcc'
    wizard = (root / 'wizard.py').read_text(encoding='utf-8')
    ui = (root / 'ui.py').read_text(encoding='utf-8')
    assert 'self.result_workspace = deepcopy(workspace)' in wizard
    assert 'preserve_published_constructor_workspace(' in ui
    assert 'wizard.result_workspace' in ui
    assert 'owned_draft_revision=(wizard.draft_revision' in ui


@pytest.mark.parametrize('strategy', ['VELES', 'POLYMARKET'])
@pytest.mark.parametrize('seed', range(25))
def test_create_and_edit_use_identical_nontrading_draft_retention(tmp_path, strategy, seed):
    repo = Repository(tmp_path)
    workspace = sample(strategy, seed)
    inactive = workspace['symbols'].pop()
    executable = validate_constructor_publish(workspace)
    folder = repo.create(executable)
    # Create: no draft previously existed; the inactive settings must remain
    # available when the bot is reopened for editing.
    first = retain_workspace_after_commit(repo, folder, workspace, executable)
    assert first['dormant_preserved']
    live, digest = repo.load(folder)
    binding = edit_workspace_binding(repo.root, folder, digest)
    reopened, revision = load_constructor_draft_snapshot(
        first['draft_path'], binding=binding)
    assert inactive in reopened['coin_overrides']
    assert validate_constructor_publish(reopened) == live
    # Edit: active values change and the old version-bound draft is migrated
    # only after the bot's optimistic repository save succeeds.
    changed = deepcopy(reopened)
    changed['name'] += ' edited'
    if strategy == 'POLYMARKET':
        changed['polymarket']['entry_seconds_before_close'] = 80
    else:
        changed['settings']['leverage'] = 5
    next_executable = validate_constructor_publish(changed)
    repo.save(folder, next_executable, digest)
    second = retain_workspace_after_commit(
        repo, folder, changed, next_executable,
        previous_path=first['draft_path'], previous_revision=revision)
    assert second['dormant_preserved']
    assert second['old_draft_removed'] is True
    assert second['draft_path'] != first['draft_path']
    assert not first['draft_path'].exists()
    assert inactive in load_constructor_draft_snapshot(
        second['draft_path'], binding=edit_workspace_binding(
            repo.root, folder, repo.load(folder)[1]))[0]['coin_overrides']
    assert not (folder / 'state.sqlite3').exists()


def test_conflicting_target_draft_preserves_both_versions(tmp_path):
    repo = Repository(tmp_path)
    cfg = sample('POLYMARKET', 7)
    cfg['symbols'].remove('SOL')
    live = validate_constructor_publish(cfg)
    folder = repo.create(live)
    _, digest = repo.load(folder)
    target = edit_workspace_path(repo.root, folder, digest)
    binding = edit_workspace_binding(repo.root, folder, digest)
    # A different editor has already saved a draft for this bot/version.
    existing = deepcopy(cfg)
    existing['name'] += ' existing editor'
    save_constructor_draft_with_revision(
        target, existing, expected_revision=None, binding=binding)
    before = target.read_bytes()
    with pytest.raises(ValueError, match='другом окне'):
        retain_workspace_after_commit(repo, folder, cfg, live)
    assert target.read_bytes() == before
    assert repo.load(folder)[0] == live


@pytest.mark.parametrize('strategy', ['VELES', 'POLYMARKET'])
@pytest.mark.parametrize('kind', ['unknown_coin_field', 'nonfinite_dormant', 'invalid_dormant_symbol',
                                  'unknown_market_field', 'bad_market_key'])
def test_publish_rejects_corrupt_inactive_workspace_before_bot_commit(strategy, kind):
    cfg = sample(strategy, 3)
    inactive = cfg['symbols'].pop()
    if kind == 'unknown_coin_field':
        cfg['coin_overrides'][inactive]['unsupported_setting'] = 1
    elif kind == 'nonfinite_dormant':
        cfg['coin_overrides'][inactive]['settings'] = {'leverage': float('nan')}
    elif kind == 'invalid_dormant_symbol':
        cfg['coin_overrides']['not a symbol!'] = cfg['coin_overrides'].pop(inactive)
    elif kind == 'unknown_market_field':
        if strategy != 'POLYMARKET':
            return
        cfg['polymarket']['overrides']['SOL:3600']['unsupported_setting'] = 1
    else:
        if strategy != 'POLYMARKET':
            return
        cfg['polymarket']['overrides']['SOL:foo'] = cfg['polymarket']['overrides'].pop('SOL:3600')
    with pytest.raises((ValueError, TypeError)):
        validate_constructor_publish(cfg)
