"""OS startup and ledger admission share one durable controller revision."""
import json
import uuid

import pytest

from bcc.config import defaults
from bcc.coordination import Coordinator
from bcc.launch_claim import issue_launch, claim_path
from bcc.lifecycle import Lifecycle
from bcc.storage import BotBusy, BotLock, Repository
from bcc.worker import run


def setup_controlled(tmp_path):
    repo = Repository(tmp_path / 'state')
    cfg = defaults('POLYMARKET')
    cfg['name'] = 'Controlled account'
    folder = repo.create(cfg)
    coord = Coordinator(repo.root / 'coordination.sqlite')
    coord.initialize()
    key = folder.relative_to(repo.bots).as_posix()
    coord.register(key)
    coord.set_desired(key, 'RUNNING')
    return repo, folder, coord, key


def test_stale_controlled_worker_cannot_create_ledger_or_leave_claim(tmp_path):
    repo, folder, coord, key = setup_controlled(tmp_path)
    revision = coord.get_registration(key)['command_version']
    coord.set_desired(key, 'STOPPED')
    token = uuid.uuid4().hex
    with BotLock(folder):
        issue_launch(folder, token)
    with pytest.raises(RuntimeError, match='Устаревшая команда'):
        run(folder, token, once=True, control_revision=revision)
    assert not (folder / 'paper.sqlite').exists()
    assert not claim_path(folder).exists()
    assert json.loads((folder / 'process.json').read_text(encoding="utf-8"))['status'] == 'ERROR'


def test_stopped_controlled_bot_cannot_be_launched_by_old_controller(tmp_path):
    repo, folder, coord, key = setup_controlled(tmp_path)
    coord.set_desired(key, 'STOPPED')
    with pytest.raises(BotBusy, match='Запуск отменён'):
        Lifecycle(repo).start(folder)
    assert not claim_path(folder).exists()


def test_revision_changes_revoke_old_launch_and_stop_running_worker(tmp_path):
    repo, folder, coord, key = setup_controlled(tmp_path)
    revision = coord.get_registration(key)['command_version']
    assert coord.launch_allowed(key, revision)
    coord.set_desired(key, 'STOPPED')
    assert not coord.launch_allowed(key, revision)
    assert not coord.desires_running(key)
    coord.set_desired(key, 'RUNNING')
    assert coord.desires_running(key)
    assert not coord.launch_allowed(key, revision)
    assert coord.launch_allowed(key, coord.get_registration(key)['command_version'])


def test_control_revoke_during_startup_releases_claimed_ledger(tmp_path, monkeypatch):
    """A rejected launch must not keep an unexpired financial lease."""
    import sqlite3
    from bcc.storage import Store, ControlRevoked
    repo, folder, coord, key = setup_controlled(tmp_path)
    token = uuid.uuid4().hex
    with BotLock(folder):
        issue_launch(folder, token)
    def revoke_after_ledger_claim(self, controller, key, revision):
        controller.set_desired(key, 'STOPPED')
        raise ControlRevoked('Control revision revoked before PAPER commit')
    monkeypatch.setattr(Store, 'bind_control_authority', revoke_after_ledger_claim)
    with pytest.raises(ControlRevoked):
        run(folder, token, once=True,
            control_revision=coord.get_registration(key)['command_version'])
    with sqlite3.connect(folder / 'paper.sqlite') as db:
        assert db.execute('SELECT claimant FROM execution_owner WHERE id=1').fetchone() == (None,)
    assert json.loads((folder / 'process.json').read_text(encoding="utf-8"))['status'] == 'ERROR'
