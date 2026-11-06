"""GUI export and local transfer use owner-wide interprocess admission."""
import threading
import zipfile
import pytest
from bcc.config import defaults
from bcc.storage import Repository, Store
from bcc.supervisor import Supervisor


def setup_bot(tmp_path):
    repo = Repository(tmp_path / 'root')
    manager = Supervisor(repo)
    cfg = defaults('GRID')
    cfg['name'] = 'Owner'
    bot = manager.create_bot(cfg)
    Store(bot, cfg['capital'])
    return manager, bot


def test_history_export_blocks_concurrent_stop(tmp_path, monkeypatch):
    supervisor, bot = setup_bot(tmp_path)
    inside = threading.Event()
    release = threading.Event()
    stopped = threading.Event()
    errors = []
    original = Store.history

    def pause(store, *args, **kwargs):
        inside.set()
        assert release.wait(5)
        return original(store, *args, **kwargs)

    monkeypatch.setattr(Store, 'history', pause)

    def snapshot():
        try:
            supervisor.export_history_csv(bot, tmp_path / 'events.csv')
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=snapshot)
    thread.start()
    try:
        assert inside.wait(5)
        stopper = threading.Thread(target=lambda: (supervisor.request_stop(bot), stopped.set()))
        stopper.start()
        assert not stopped.wait(.1)
    finally:
        release.set()
        thread.join(7)
    stopper.join(7)
    assert not errors
    assert stopped.is_set()
    assert (tmp_path / 'events.csv').is_file()


def test_history_export_rejects_retirement_and_keeps_destination(tmp_path):
    manager, bot = setup_bot(tmp_path)
    target = tmp_path / 'trades.csv'
    target.write_bytes(b'original')
    manager.coordinator.begin_retirement(manager._key(bot))
    with pytest.raises(ValueError, match='удаляется'):
        manager.export_history_csv(bot, target)
    assert target.read_bytes() == b'original'


def test_transfer_import_rejects_retiring_local_source(tmp_path):
    manager, bot = setup_bot(tmp_path)
    archive = tmp_path / 'copy.zip'
    manager.export_bots([bot], archive)
    manager.coordinator.begin_retirement(manager._key(bot))
    with pytest.raises(ValueError, match='удаляется'):
        manager.import_bots(archive)


def test_history_rejects_export_inside_my_bots(tmp_path):
    manager, bot = setup_bot(tmp_path)
    with pytest.raises(ValueError):
        manager.export_history_csv(bot, manager.repo.bots / 'trade.csv')
