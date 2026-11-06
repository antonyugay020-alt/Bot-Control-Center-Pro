"""Unified operator path: Qt callbacks must not create unregistered owners."""
from pathlib import Path

import pytest

from bcc.config import defaults
from bcc.storage import Repository, Store
from bcc.supervisor import Supervisor
from bcc.transfer import export_bots


@pytest.mark.parametrize('strategy', ['VELES', 'GRID', 'PUMP_DUMP', 'POLYMARKET'])
def test_create_edit_copy_transfer_registers_independent_owners(tmp_path, strategy):
    repo = Repository(tmp_path / 'workspace')
    supervisor = Supervisor(repo)
    cfg = defaults(strategy)
    cfg['name'] = 'Architecture ' + strategy
    original = supervisor.create_bot(cfg)
    source_key = supervisor._key(original)
    assert supervisor.coordinator.get_registration(source_key)['desired'] == 'STOPPED'
    current, digest = repo.load(original)
    edited = {**current, 'name': current['name'] + ' edited'}
    supervisor.save_bot(original, edited, digest)
    assert repo.load(original)[0]['name'] == edited['name']
    copied = supervisor.copy_bot(original, history=False)
    assert copied != original
    assert supervisor.coordinator.get_registration(supervisor._key(copied))['desired'] == 'STOPPED'
    assert not (copied / 'paper.sqlite').exists()
    backup = tmp_path / 'export.zip'
    export_bots(repo, [original], backup, history=False)
    imported = supervisor.import_bots(backup, history=False)
    assert len(imported) == 1
    assert imported[0] not in (original, copied)
    assert supervisor.coordinator.get_registration(supervisor._key(imported[0]))['desired'] == 'STOPPED'
    assert len({supervisor._key(path) for path in (original, copied, imported[0])}) == 3


def test_new_copy_with_history_is_isolated_and_retirement_blocks_mutations(tmp_path):
    repo = Repository(tmp_path / 'workspace')
    supervisor = Supervisor(repo)
    cfg = defaults('VELES')
    cfg['name'] = 'Account owner'
    source = supervisor.create_bot(cfg)
    Store(source, cfg['capital'])
    copied = supervisor.copy_bot(source, history=True)
    assert Store(copied).read()['cash'] == Store(source).read()['cash']
    assert (copied / 'paper.sqlite').is_file()
    key = supervisor._key(source)
    supervisor.coordinator.begin_retirement(key)
    config, digest = repo.load(source)
    with pytest.raises(ValueError, match='удаляется|удалён'):
        supervisor.save_bot(source, config, digest)
    with pytest.raises(ValueError, match='удаляется|удалён'):
        supervisor.copy_bot(source, history=False)
    assert Store(copied).read()['positions'] == {}


def test_gui_and_native_check_use_one_operator_authority():
    root = Path(__file__).resolve().parents[1]
    for filename in ('bcc/ui.py', 'bcc/gui_check.py'):
        code = (root / filename).read_text(encoding='utf-8')
        for bypass in ('self.repo.create(', 'self.repo.save(', 'self.repo.copy(',
                       'window.repo.create(', 'window.repo.save('):
            assert bypass not in code, f'{filename} bypasses supervisor through {bypass}'
    code = (root / 'bcc/ui.py').read_text(encoding='utf-8')
    assert 'self.supervisor.import_bots(' in code
    assert 'self.supervisor.create_bot(' in code
    assert 'self.supervisor.save_bot(' in code
    assert 'self.supervisor.copy_bot(' in code
