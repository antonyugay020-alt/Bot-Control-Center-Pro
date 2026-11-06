"""Regression: lifecycle may never see a partial clone or half-deleted bot."""

import json
import shutil
from pathlib import Path

import pytest

from bcc.config import defaults
from bcc.lifecycle import Lifecycle
from bcc.storage import BotBusy, BotLock, Repository, Store


def new_repo(tmp_path):
    repo = Repository(tmp_path / "app")
    cfg = defaults("POLYMARKET")
    cfg["name"] = "Safe copy / delete"
    bot = repo.create(cfg)
    Store(bot, 500)
    return repo, bot


def test_copy_is_not_published_until_complete_sqlite_backup(tmp_path, monkeypatch):
    repo, bot = new_repo(tmp_path)
    (bot / "engine.log").write_bytes(b"copied log")
    published = repo.publish

    def verified_publish(staged):
        assert staged.name.startswith(".creating-")
        assert [p for p, _ in repo.list()] == [bot]
        assert (staged / "engine.log").read_bytes() == b"copied log"
        Store(staged).audit()
        assert (staged / Store.MARKER).is_file()
        return published(staged)

    monkeypatch.setattr(repo, "publish", verified_publish)
    replica = repo.copy(bot, history=True)
    assert replica != bot
    assert Store(replica).read() == Store(bot).read()
    first = json.loads((bot / Store.MARKER).read_text(encoding="utf-8"))["account_id"]
    second = json.loads((replica / Store.MARKER).read_text(encoding="utf-8"))["account_id"]
    assert first != second


def test_failed_copy_never_exposes_partial_bot(tmp_path, monkeypatch):
    repo, bot = new_repo(tmp_path)

    def truncated_backup(self, target):
        Path(target).write_bytes(b"partial sqlite")
        raise OSError("backup interrupted")

    monkeypatch.setattr(Store, "backup", truncated_backup)
    with pytest.raises(OSError, match="backup interrupted"):
        repo.copy(bot, history=True)
    assert [p for p, _ in repo.list()] == [bot]
    assert not list(repo.bots.glob("*/.creating-*"))
    Store(bot).audit()


def test_delete_marker_blocks_late_launch_between_unlock_and_rename(tmp_path, monkeypatch):
    repo, bot = new_repo(tmp_path)
    old_rename = Path.rename
    inspected = []

    def guarded_rename(self, target):
        if self == bot:
            assert (bot / ".deleting").is_file()
            with pytest.raises(BotBusy, match="удаляется"):
                with BotLock(bot):
                    pass
            with pytest.raises(BotBusy):
                Lifecycle(repo).start(bot)
            inspected.append(True)
        return old_rename(self, target)

    monkeypatch.setattr(Path, "rename", guarded_rename)
    repo.delete(bot)
    assert inspected == [True]
    assert not bot.exists()
    assert not list(repo.bots.glob("*/.deleted-*"))


def test_rename_error_rolls_back_delete_marker_and_retains_original(tmp_path, monkeypatch):
    repo, bot = new_repo(tmp_path)
    real_rename = Path.rename

    def fail_rename(self, target):
        if self == bot:
            raise PermissionError("rename denied")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", fail_rename)
    with pytest.raises(PermissionError, match="rename denied"):
        repo.delete(bot)
    assert (bot / "config.json").exists()
    assert not (bot / ".deleting").exists()
    with BotLock(bot):
        Store(bot).audit()


def test_partial_cleanup_keeps_remaining_data_isolated(tmp_path, monkeypatch):
    repo, bot = new_repo(tmp_path)
    real_rmtree = shutil.rmtree

    def partial_remove(path, *args, **kwargs):
        if Path(path).name.startswith(".deleted-"):
            (Path(path) / "config.json").unlink()
            raise PermissionError("partial tomb cleanup denied")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", partial_remove)
    from bcc.storage import AccountingError
    with pytest.raises(AccountingError, match="изолированы"):
        repo.delete(bot)
    assert not bot.exists()
    assert repo.list() == []
    tombs = list(repo.bots.glob("*/.deleted-*"))
    assert len(tombs) == 1
    assert (tombs[0] / "paper.sqlite").exists()
    with pytest.raises(BotBusy):
        with BotLock(tombs[0]):
            pass


def test_full_clone_preserves_open_polymarket_position_and_frozen_rules(tmp_path):
    """A clone is an independent, audited snapshot, not an empty reset."""
    from bcc.worker import process_polymarket
    from test_poly_contract_seconds import ScheduledSource, cfg

    repo = Repository(tmp_path / "app")
    configuration = cfg()
    configuration["symbols"] = ["BTC"]
    configuration["polymarket"]["durations"] = [300]
    bot = repo.create(configuration)
    store = Store(bot, 500)
    source = ScheduledSource(42)
    with store.transaction() as (state, events):
        process_polymarket(configuration, state, events, source, source.now)
    assert store.read()["positions"]
    initial_history = store.history()
    original_positions = store.read()["positions"]

    replica = repo.copy(bot, history=True)
    restored = Store(replica)
    restored.audit()
    assert restored.read()["positions"] == original_positions
    assert restored.history() == initial_history
    assert len(restored.history()) == 1

    replica_cfg, digest = repo.load(replica)
    replica_cfg["name"] = "Independent copy"
    replica_cfg["polymarket"]["entry_seconds_before_close"] = 50
    repo.save(replica, replica_cfg, digest)
    assert Store(replica).read()["positions"] == original_positions
    assert Store(bot).read()["positions"] == original_positions
    assert repo.load(bot)[0]["polymarket"]["entry_seconds_before_close"] == 45
