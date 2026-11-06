"""Deletion identity fencing across delayed registration and process crashes."""
import pytest
from bcc.config import defaults
from bcc.storage import BotLock, BotBusy, Repository, atomic_json
from bcc.supervisor import Supervisor


def bot_config():
    cfg = defaults('VELES')
    cfg['name'] = 'Retirement recovery'
    return cfg


def test_unregistered_bot_cannot_be_resurrected_after_delete(tmp_path):
    repo = Repository(tmp_path)
    folder = repo.create(bot_config())
    supervisor = Supervisor(repo)
    key = supervisor._key(folder)
    assert supervisor.coordinator.get_registration(key) is None
    supervisor.delete_bot(folder)
    assert not folder.exists()
    assert supervisor.coordinator.get_registration(key) is None
    with pytest.raises(ValueError, match='уже удалён'):
        supervisor.coordinator.submit_desired(key, 'RUNNING')
    with pytest.raises(ValueError, match='уже удалён'):
        supervisor.coordinator.register(key)


def test_interrupted_deletion_marker_can_be_recovered_only_by_delete(tmp_path):
    repo = Repository(tmp_path)
    folder = repo.create(bot_config())
    supervisor = Supervisor(repo)
    key = supervisor._key(folder)
    supervisor.coordinator.begin_retirement(key)
    atomic_json(folder / '.deleting', {'operation':'delete','id':'old-operation'})
    with pytest.raises(BotBusy):
        with BotLock(folder):
            pass
    supervisor.delete_bot(folder)
    assert not folder.exists()
    with pytest.raises(ValueError, match='уже удалён'):
        supervisor.coordinator.submit_desired(key, 'RUNNING')


def test_corrupt_interrupted_deletion_remains_fenced(tmp_path):
    repo = Repository(tmp_path)
    folder = repo.create(bot_config())
    supervisor = Supervisor(repo)
    supervisor.coordinator.begin_retirement(supervisor._key(folder))
    (folder / '.deleting').write_text('not-json', encoding='utf-8')
    with pytest.raises(Exception, match='маркер'):
        supervisor.delete_bot(folder)
    assert folder.exists()
    with pytest.raises(BotBusy):
        with BotLock(folder):
            pass
