"""Real account/registry transfer races, with no worker or trading API calls."""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from bcc.config import defaults
from bcc.storage import BotBusy, BotLock, Repository, Store
from bcc.supervisor import Supervisor


def setup_batch(tmp_path):
    repo = Repository(tmp_path / 'accounts')
    owner = Supervisor(repo)
    originals = []
    for strategy in ('VELES', 'POLYMARKET'):
        cfg = defaults(strategy)
        cfg['name'] = 'Transfer ' + strategy
        folder = owner.create_bot(cfg)
        Store(folder, cfg['capital'])
        originals.append(folder)
    archive = tmp_path / 'both.zip'
    owner.export_bots(originals, archive)
    return owner, originals, archive


@pytest.mark.parametrize('operation', ['edit', 'copy', 'export', 'history', 'import'])
def test_pending_start_is_not_a_stopped_owner(tmp_path, operation):
    owner, originals, archive = setup_batch(tmp_path)
    bot = originals[0]
    before = {p.name: p.read_bytes() for p in (bot / 'config.json', bot / 'paper.sqlite')}
    cfg, digest = owner.repo.load(bot)
    cfg['name'] += ' edit'
    owner.request_start(bot)  # Durable command; no OS worker exists yet.
    actions = {
        'edit': lambda: owner.save_bot(bot, cfg, digest),
        'copy': lambda: owner.copy_bot(bot, history=True),
        'export': lambda: owner.export_bots(originals, tmp_path / 'later.zip'),
        'history': lambda: owner.export_history_csv(bot, tmp_path / 'later.csv'),
        'import': lambda: owner.import_bots(archive),
    }
    with pytest.raises(BotBusy, match='запуск'):
        actions[operation]()
    assert len(owner.repo.list()) == 2
    assert all((bot / name).read_bytes() == data for name, data in before.items())
    assert not (tmp_path / 'later.zip').exists()
    assert not (tmp_path / 'later.csv').exists()
    assert owner.coordinator.get_registration(owner._key(bot))['desired'] == 'RUNNING'
    owner.request_stop(bot)
    actions[operation]()


@pytest.mark.parametrize('phase', ['stage', 'publish', 'rollback'])
def test_all_origins_remain_fenced_until_batch_finishes(tmp_path, monkeypatch, phase):
    owner, originals, archive = setup_batch(tmp_path)
    # A separate controller has different in-process locks. Only the real
    # cross-process command gates can serialize its accepted START.
    competing = Supervisor(Repository(owner.repo.root))
    entered, release, command_done = (threading.Event() for _ in range(3))
    method = 'prepare' if phase == 'stage' else 'publish'
    original = getattr(owner.repo, method)
    paused = False

    def pause(*args, **kwargs):
        nonlocal paused
        if not paused:
            paused = True
            # Both origins must be owned even before the first staging write.
            for bot in originals:
                with pytest.raises(BotBusy):
                    with BotLock(bot):
                        pass
            entered.set()
            assert release.wait(7)
            if phase == 'rollback':
                raise OSError('interrupted publication')
        return original(*args, **kwargs)

    monkeypatch.setattr(owner.repo, method, pause)
    with ThreadPoolExecutor(max_workers=2) as pool:
        imported = pool.submit(owner.import_bots, archive)
        try:
            assert entered.wait(7)
            def start():
                competing.request_start(originals[-1])
                command_done.set()
            command = pool.submit(start)
            assert not command_done.wait(.15)
            assert competing.coordinator.get_registration(
                competing._key(originals[-1]))['desired'] == 'STOPPED'
        finally:
            release.set()
        if phase == 'rollback':
            with pytest.raises(OSError, match='interrupted'):
                imported.result(timeout=10)
            assert len(owner.repo.list()) == 2
            assert not list(owner.repo.bots.glob('*/.creating-*'))
        else:
            restored = imported.result(timeout=10)
            assert len(restored) == 2
            for bot in restored:
                Store(bot).audit()
                assert owner.coordinator.get_registration(owner._key(bot))['desired'] == 'STOPPED'
        command.result(timeout=10)
        assert command_done.is_set()
    for bot in originals:
        with BotLock(bot):
            Store(bot).audit()


def test_duplicate_origins_and_reverse_batches_do_not_deadlock(tmp_path):
    owner, originals, archive = setup_batch(tmp_path)
    reversed_archive = tmp_path / 'reversed.zip'
    # Repeated entries are separate intended copies but one local origin lease.
    owner.export_bots(list(reversed(originals)) + [originals[0]], reversed_archive)
    other = Supervisor(Repository(owner.repo.root))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(owner.import_bots, archive)
        second = pool.submit(other.import_bots, reversed_archive)
        restored = first.result(timeout=12) + second.result(timeout=12)
    assert len(restored) == 5
    assert len(owner.repo.list()) == 7
    for folder in restored + originals:
        Store(folder).audit()


def test_failed_admission_releases_previously_admitted_origins(tmp_path):
    owner, originals, archive = setup_batch(tmp_path)
    owner.coordinator.begin_retirement(owner._key(originals[-1]))
    with pytest.raises(ValueError, match='удаляется'):
        owner.import_bots(archive)
    assert len(owner.repo.list()) == 2
    # Includes releasing the OS gate entered before retirement was discovered.
    Supervisor(Repository(owner.repo.root)).request_start(originals[0])
    for bot in originals:
        with BotLock(bot):
            Store(bot).audit()
