"""The controller/worker OS-lock handoff must not race a legitimate child."""
import threading
import time

import pytest

from bcc.config import defaults
from bcc.launch_claim import claim_path, issue_launch, worker_startup_lease
from bcc.storage import BotBusy, BotLock, Repository


def test_worker_waits_for_controller_to_release_lock(tmp_path):
    repo = Repository(tmp_path / 'owner')
    cfg = defaults('VELES')
    cfg['name'] = 'Handoff owner'
    folder = repo.create(cfg)
    token = 'a' * 32
    entered = threading.Event()
    finished = threading.Event()
    errors = []

    def worker():
        try:
            with worker_startup_lease(folder, token, timeout=2):
                entered.set()
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    with BotLock(folder):
        issue_launch(folder, token)
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        assert not entered.wait(.1), 'Worker acquired controller-owned OS lock'
    assert finished.wait(3)
    thread.join(1)
    assert not errors
    assert entered.is_set()


def test_revoked_worker_token_cannot_wait_into_new_owner_epoch(tmp_path):
    repo = Repository(tmp_path / 'owner')
    cfg = defaults('VELES')
    cfg['name'] = 'Handoff owner'
    folder = repo.create(cfg)
    old_token = 'a' * 32
    new_token = 'b' * 32
    with BotLock(folder):
        issue_launch(folder, old_token)
        claim_path(folder).unlink()
        issue_launch(folder, new_token)
        with pytest.raises(BotBusy):
            with worker_startup_lease(folder, old_token, timeout=.1):
                pytest.fail('Revoked worker acquired the PAPER lock')


def test_unconfirmed_worker_never_acquires_paper_lock(tmp_path):
    repo = Repository(tmp_path / 'owner')
    cfg = defaults('VELES')
    cfg['name'] = 'Handoff owner'
    folder = repo.create(cfg)
    with pytest.raises(BotBusy):
        with worker_startup_lease(folder, 'c' * 32, timeout=.1):
            pytest.fail('Unconfirmed worker entered PAPER transaction')
