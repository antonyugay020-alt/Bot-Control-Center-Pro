"""Operator edits/copies must be ordered with STOP and retirement across processes."""
from threading import Event, Thread

import pytest

from bcc.config import defaults
from bcc.coordination import Coordinator
from bcc.storage import Repository
from bcc.supervisor import Supervisor


@pytest.mark.parametrize('operation', ['edit', 'copy'])
def test_operator_mutation_precedes_or_follows_retirement_without_overlap(tmp_path, operation):
    repo = Repository(tmp_path)
    sup = Supervisor(repo)
    cfg = defaults('VELES'); cfg['name'] = 'concurrent owner'
    original = sup.create_bot(cfg)
    key = sup._key(original)
    started, finish = Event(), Event()
    failures = []
    original_method = repo.save if operation == 'edit' else repo.copy

    def blocked(*args, **kwargs):
        started.set()
        assert finish.wait(8)
        return original_method(*args, **kwargs)

    if operation == 'edit':
        repo.save = blocked
        config, digest = repo.load(original)
        config['name'] = 'edited owner'
        fn = lambda: sup.save_bot(original, config, digest)
    else:
        repo.copy = blocked
        fn = lambda: sup.copy_bot(original)

    def mutate():
        try:
            fn()
        except BaseException as exc:
            failures.append(exc)

    mutation = Thread(target=mutate)
    mutation.start()
    assert started.wait(5)
    retired = Event()
    def retire():
        try:
            Coordinator(sup.coordinator.path).begin_retirement(key)
        except BaseException as exc:
            failures.append(exc)
        finally:
            retired.set()
    retirement = Thread(target=retire)
    retirement.start()
    # The second process must not publish the retirement fence while the
    # operator is in its protected filesystem/ledger edit.
    assert not retired.wait(0.1)
    finish.set()
    mutation.join(8); retirement.join(8)
    assert not mutation.is_alive() and not retirement.is_alive()
    assert not failures
    assert sup.coordinator.get_registration(key)['retiring'] is True
    with pytest.raises(ValueError, match='удаляется|удалён'):
        fn()


def test_strict_registration_rejects_retiring_identity(tmp_path):
    c = Coordinator(tmp_path / 'coord.sqlite')
    c.initialize(); c.register('VELES/a')
    c.begin_retirement('VELES/a')
    with pytest.raises(ValueError, match='удаляется'):
        c.register('VELES/a')


def test_operator_edit_does_not_block_independent_bot_stop(tmp_path):
    """Per-owner serialization must never become a global fleet mutex."""
    repo = Repository(tmp_path)
    supervisor = Supervisor(repo)
    cfg = defaults('VELES'); cfg['name'] = 'first'
    first = supervisor.create_bot(cfg)
    cfg['name'] = 'second'
    second = supervisor.create_bot(cfg)
    changed, digest = repo.load(first)
    changed['name'] = 'renamed first'
    entered, release = Event(), Event()
    original_save = repo.save
    errors = []

    def pause_save(*args, **kwargs):
        entered.set()
        assert release.wait(8)
        return original_save(*args, **kwargs)

    repo.save = pause_save
    def writer():
        try:
            supervisor.save_bot(first, changed, digest)
        except BaseException as exc:
            errors.append(exc)

    operation = Thread(target=writer)
    operation.start()
    assert entered.wait(5)
    other_controller = Coordinator(supervisor.coordinator.path)
    other_controller.submit_desired(supervisor._key(second), 'STOPPED')
    assert other_controller.get_registration(supervisor._key(first))['retiring'] is False
    release.set(); operation.join(8)
    assert not operation.is_alive() and not errors
    assert repo.load(first)[0]['name'] == 'renamed first'


def test_capital_edit_and_stop_share_commit_order(tmp_path, monkeypatch):
    """Config and ledger change cannot straddle an acknowledged STOP."""
    from bcc.storage import Store
    repo = Repository(tmp_path)
    sup = Supervisor(repo)
    cfg = defaults('VELES'); cfg['name'] = 'account'
    folder = sup.create_bot(cfg)
    Store(folder, cfg['capital'])
    revised, digest = repo.load(folder)
    revised['capital'] += 50

    entering, release = Event(), Event()
    original_adjust = Store.adjust_capital

    def hold_adjust(self, *args, **kwargs):
        entering.set()
        assert release.wait(8)
        return original_adjust(self, *args, **kwargs)

    monkeypatch.setattr(Store, 'adjust_capital', hold_adjust)
    errors = []
    def writer():
        try:
            sup.save_bot(folder, revised, digest)
        except BaseException as exc:
            errors.append(exc)
    thread = Thread(target=writer)
    thread.start()
    assert entering.wait(5)
    stop_done = Event()
    def stop():
        try:
            Coordinator(sup.coordinator.path).submit_desired(sup._key(folder), 'STOPPED')
        except BaseException as exc:
            errors.append(exc)
        finally:
            stop_done.set()
    stopper = Thread(target=stop)
    stopper.start()
    assert not stop_done.wait(0.1)
    release.set()
    thread.join(8); stopper.join(8)
    assert not thread.is_alive() and not stopper.is_alive() and not errors
    assert repo.load(folder)[0]['capital'] == revised['capital']
    assert Store(folder).effective_capital() == revised['capital']
    Store(folder).audit()
