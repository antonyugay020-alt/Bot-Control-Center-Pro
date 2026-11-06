"""Cross-strategy publication is pure and uses the same Qt commit gateway."""
from copy import deepcopy
from pathlib import Path
import random

import pytest

from bcc.config import STRATEGIES, defaults, validate
from bcc.constructor_transaction import publish_wizard_configuration
from bcc.constructor_edit_workspace import preserve_published_constructor_workspace
from bcc.storage import Repository


@pytest.mark.parametrize('strategy', STRATEGIES)
@pytest.mark.parametrize('seed', range(12))
def test_all_strategies_create_edit_roundtrip_without_workers(tmp_path, strategy, seed):
    rng = random.Random(20261106028 + seed)
    repo = Repository(tmp_path / 'repo')
    cfg = defaults(strategy)
    cfg['name'] = f'Qt lifecycle {strategy} {seed}'
    cfg['capital'] = rng.randint(500, 1500)
    if strategy == 'POLYMARKET':
        cfg['symbols'] = ['BTC', 'ETH']
        cfg['polymarket']['entry_signal_mode'] = 'before_close'
        cfg['polymarket']['entry_seconds_before_close'] = 90
        cfg['polymarket']['entry_window_seconds'] = 10
        cfg['polymarket']['overrides']['ETH:900'] = {
            'entry_seconds_before_close': rng.randrange(40, 70)}
    before = deepcopy(cfg)
    proposed = publish_wizard_configuration(cfg)
    assert cfg == before
    assert proposed == validate(proposed)
    folder = repo.create(proposed)
    saved, digest = repo.load(folder)
    assert saved == proposed
    if strategy == 'POLYMARKET':
        assert saved['polymarket']['entry_seconds_before_close'] == 90
        assert saved['polymarket']['overrides']['ETH:900'] == cfg['polymarket']['overrides']['ETH:900']
    status = preserve_published_constructor_workspace(repo, folder, cfg, saved)
    assert not status['dormant_preserved']
    edited = deepcopy(saved)
    edited['name'] += ' edited'
    updated = publish_wizard_configuration(edited)
    repo.save(folder, updated, digest)
    assert repo.load(folder)[0] == updated
    assert not (folder / 'process.json').exists()
    assert not (folder / 'paper.sqlite').exists()


@pytest.mark.parametrize('strategy', STRATEGIES)
def test_publication_rejects_invalid_and_does_not_mutate_workspace(strategy):
    cfg = defaults(strategy)
    cfg['name'] = 'Safe publication'
    cfg['capital'] = -10
    before = deepcopy(cfg)
    with pytest.raises((ValueError, TypeError, KeyError)):
        publish_wizard_configuration(cfg)
    assert cfg == before


def test_wizard_commit_uses_shared_publication_gateway():
    text = (Path(__file__).resolve().parents[1] / 'bcc' / 'wizard.py').read_text('utf-8')
    commit = text.split('    def commit(self):', 1)[1]
    assert 'publish_wizard_configuration(workspace)' in commit
    assert 'validate_constructor_publish(workspace)' not in commit
