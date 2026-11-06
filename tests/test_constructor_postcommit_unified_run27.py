"""Create/Edit/Open Draft share one non-trading post-publication contract.

Randomized, isolated PAPER repositories only. Never launch a worker or an order.
"""
from copy import deepcopy
from pathlib import Path
import random

import pytest

from bcc.config import defaults, resolve_poly_market
from bcc.constructor_edit_workspace import (
    edit_workspace_binding, preserve_published_constructor_workspace,
)
from bcc.constructor_session import (
    load_constructor_draft_snapshot, save_constructor_draft_with_revision,
)
from bcc.constructor_transaction import validate_constructor_publish
from bcc.storage import Repository


def _workspace(strategy, seed):
    rng = random.Random(20261106027 + seed)
    cfg = defaults(strategy)
    cfg['name'] = f'Unified constructor lifecycle {strategy} {seed}'
    if strategy == 'POLYMARKET':
        cfg['symbols'] = ['BTC', 'ETH', 'SOL']
        cfg['polymarket'].update(entry_signal_mode='before_close',
                                 entry_seconds_before_close=90,
                                 entry_window_seconds=10)
        before = rng.randrange(35, 80)
        cfg['polymarket']['overrides']['SOL:900'] = {
            'entry_seconds_before_close': before, 'entry_window_seconds': 5}
        cfg['symbols'].remove('SOL')
        return cfg, 'SOL', before
    cfg['symbols'] = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']
    leverage = rng.randrange(2, 12)
    cfg['coin_overrides']['SOLUSDT'] = {'settings': {'leverage': leverage}}
    cfg['symbols'].remove('SOLUSDT')
    return cfg, 'SOLUSDT', leverage


@pytest.mark.parametrize('strategy', ['VELES', 'POLYMARKET'])
@pytest.mark.parametrize('seed', range(30))
def test_open_draft_create_edit_roundtrip_keeps_external_file_and_dormant_policy(
        tmp_path, strategy, seed):
    repo = Repository(tmp_path / 'repo')
    workspace, inactive, value = _workspace(strategy, seed)
    source = tmp_path / f'external_{strategy}_{seed}.json'
    save_constructor_draft_with_revision(source, workspace, expected_revision=None)
    original = source.read_bytes()
    restored, _ = load_constructor_draft_snapshot(source, expected_strategy=strategy)
    published = validate_constructor_publish(restored)
    folder = repo.create(published)
    outcome = preserve_published_constructor_workspace(repo, folder, restored, published)
    assert outcome['dormant_preserved'] and not outcome['not_applicable']
    assert source.read_bytes() == original
    assert repo.load(folder)[0] == published
    assert not (folder / 'paper.sqlite').exists()
    _, digest = repo.load(folder)
    rebound, revision = load_constructor_draft_snapshot(
        outcome['draft_path'], expected_strategy=strategy,
        binding=edit_workspace_binding(repo.root, folder, digest))
    assert inactive in rebound['coin_overrides'] if strategy == 'VELES' else inactive not in published['symbols']
    assert validate_constructor_publish(rebound) == published
    rebound['symbols'].append(inactive)
    if strategy == 'POLYMARKET':
        assert resolve_poly_market(rebound, 'SOL', 900)['entry_seconds_before_close'] == value
    else:
        assert rebound['coin_overrides'][inactive]['settings']['leverage'] == value
    # Edit the published bot, then rebase its dormant policies to the new
    # digest-bound draft. The original imported source remains untouched.
    rebound['symbols'].remove(inactive)
    rebound['name'] += ' edited'
    next_published = validate_constructor_publish(rebound)
    repo.save(folder, next_published, digest)
    next_result = preserve_published_constructor_workspace(
        repo, folder, rebound, next_published,
        owned_draft_path=outcome['draft_path'], owned_draft_revision=revision)
    assert next_result['dormant_preserved']
    assert next_result['old_draft_removed'] is True
    assert not outcome['draft_path'].exists()
    assert source.read_bytes() == original
    assert repo.load(folder)[0] == next_published


@pytest.mark.parametrize('strategy', ['VELES', 'POLYMARKET'])
def test_external_previous_path_is_rejected_without_removing_source(tmp_path, strategy):
    repo = Repository(tmp_path / 'repo')
    workspace, _, _ = _workspace(strategy, 7)
    published = validate_constructor_publish(workspace)
    folder = repo.create(published)
    external = tmp_path / 'do_not_delete.json'
    external.write_text('external-user-document', encoding='utf-8')
    with pytest.raises(ValueError, match='внешнего'):
        preserve_published_constructor_workspace(
            repo, folder, workspace, published, owned_draft_path=external)
    assert external.read_text(encoding='utf-8') == 'external-user-document'
    assert repo.load(folder)[0] == published


@pytest.mark.parametrize('strategy', ['GRID', 'PUMP_DUMP', 'EMA', 'MACD'])
def test_unrelated_strategies_skip_dormant_policy_migration(strategy, tmp_path):
    cfg = defaults(strategy)
    result = preserve_published_constructor_workspace(
        None, None, cfg, cfg)
    assert result['not_applicable'] and result['draft_path'] is None


def test_qt_create_edit_open_draft_share_postcommit_entrypoint():
    ui = (Path(__file__).resolve().parents[1] / 'bcc' / 'ui.py').read_text(encoding='utf-8')
    for start, end in (('def create_bot(self):', 'def _preserve_constructor_workspace'),
                       ('def edit(self, *args):', 'def view_settings(self, *args):'),
                       ('def open_draft(self):', 'def export_bots(self, all_bots):')):
        region = ui.split(start, 1)[1].split(end, 1)[0]
        assert 'self._preserve_constructor_workspace(' in region
    imported = ui.split('def open_draft(self):', 1)[1].split('def export_bots(', 1)[0]
    assert 'owned_draft_path=' not in imported
    assert 'self._preserve_constructor_workspace(self.current, wizard)' in imported
