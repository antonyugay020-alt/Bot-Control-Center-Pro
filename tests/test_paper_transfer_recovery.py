"""Hard failures and atomicity for preserving PAPER account ownership/history."""
import json
import zipfile

import pytest

from bcc.config import defaults
from bcc.storage import AccountingError, BotBusy, BotLock, Repository, Store, guard_paper_recovery
from bcc.transfer import export_bots, import_bots


def poly_config():
    cfg = defaults("POLYMARKET")
    cfg["name"] = "Test Polymarket"
    return cfg


def test_initialized_account_never_reset_if_database_missing(tmp_path):
    repo = Repository(tmp_path / "data")
    bot = repo.create(poly_config())
    Store(bot, 500)
    assert (bot / Store.MARKER).exists()
    (bot / "paper.sqlite").unlink()
    with pytest.raises(AccountingError, match="счёт|Существующий"):
        Store(bot, 500)
    assert not (bot / "paper.sqlite").exists()
    with pytest.raises(AccountingError):
        guard_paper_recovery(bot)
    assert not (bot / "paper.sqlite").exists()
    before = (bot / "config.json").read_bytes()
    cfg, digest = repo.load(bot)
    cfg["name"] = "after"
    with pytest.raises(AccountingError):
        repo.save(bot, cfg, digest)
    assert (bot / "config.json").read_bytes() == before
    with pytest.raises(AccountingError):
        repo.copy(bot, history=True)
    with pytest.raises(AccountingError):
        export_bots(repo, [bot], tmp_path / "loss.zip", history=True)
    assert not (tmp_path / "loss.zip").exists()


def test_existing_empty_sqlite_does_not_get_initialized(tmp_path):
    path = tmp_path / "paper.sqlite"
    path.write_bytes(b"corrupted")
    with pytest.raises(Exception):
        Store(tmp_path, 500)
    assert path.read_bytes() == b"corrupted"
    assert not (tmp_path / Store.MARKER).exists()


def test_export_import_history_and_independent_account_identity(tmp_path):
    repo = Repository(tmp_path / "data")
    old = repo.create(poly_config())
    Store(old, 500)
    original_id = json.loads((old / Store.MARKER).read_text(encoding="utf-8"))["account_id"]
    target_zip = tmp_path / "history.zip"
    export_bots(repo, [old], target_zip, history=True)
    with zipfile.ZipFile(target_zip) as z:
        index = json.loads(z.read("manifest.json"))
    assert index["includes_history"] is True
    assert index["bots"][0]["ledger_present"] is True
    restored = import_bots(repo, target_zip, history=True)[0]
    Store(restored).audit()
    restored_id = json.loads((restored / Store.MARKER).read_text(encoding="utf-8"))["account_id"]
    assert restored_id != original_id
    assert (restored / "paper.sqlite").exists()


def test_import_history_missing_ledger_fails_before_any_bot_visible(tmp_path):
    repo = Repository(tmp_path / "data")
    bot = repo.create(poly_config())
    Store(bot, 500)
    archive = tmp_path / "incomplete.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("manifest.json", json.dumps({"format": 1, "includes_history": True,
            "bots": [{"prefix": "bot_0", "ledger_present": True}]}))
        z.write(bot / "config.json", "bot_0/config.json")
    before = [p for p, _ in repo.list()]
    with pytest.raises(ValueError, match="истории"):
        import_bots(repo, archive, history=True)
    assert [p for p, _ in repo.list()] == before
    assert not list(repo.bots.glob("*/.creating-*"))


def test_old_import_archive_without_proof_requires_parameters_only(tmp_path):
    repo = Repository(tmp_path / "data")
    archive = tmp_path / "legacy.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("manifest.json", json.dumps({"format": 1,
            "bots": [{"prefix": "bot_0"}]}))
        z.writestr("bot_0/config.json", json.dumps(poly_config()))
    with pytest.raises(ValueError, match="не подтверждает"):
        import_bots(repo, archive, history=True)
    fresh = import_bots(repo, archive, history=False)
    assert len(fresh) == 1 and not (fresh[0] / "paper.sqlite").exists()


def test_external_source_path_from_import_never_accessed(tmp_path):
    repo = Repository(tmp_path / "data")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "engine.lock").write_bytes(b"DO-NOT-TOUCH")
    archive = tmp_path / "untrusted.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("manifest.json", json.dumps({"format": 1,
            "bots": [{"prefix": "bot_0", "source_path": str(outside)}]}))
        z.writestr("bot_0/config.json", json.dumps(poly_config()))
    created = import_bots(repo, archive, history=False)
    assert len(created) == 1
    assert (outside / "engine.lock").read_bytes() == b"DO-NOT-TOUCH"


def test_staged_batch_not_published_on_second_invalid_bot(tmp_path):
    repo = Repository(tmp_path / "data")
    archive = tmp_path / "batch.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("manifest.json", json.dumps({"format": 1,
            "bots": [{"prefix": "bot_0"}, {"prefix": "bot_1"}]}))
        z.writestr("bot_0/config.json", json.dumps(poly_config()))
        z.writestr("bot_1/config.json", "{malformed")
    with pytest.raises(ValueError):
        import_bots(repo, archive, history=False)
    assert repo.list() == []
    assert not list(repo.bots.glob("*/.creating-*"))


def test_uninitialized_export_roundtrip_history_true(tmp_path):
    repo = Repository(tmp_path / "data")
    bot = repo.create(poly_config())
    zipped = tmp_path / "untouched.zip"
    export_bots(repo, [bot], zipped, history=True)
    imported = import_bots(repo, zipped, history=True)[0]
    assert not (imported / "paper.sqlite").exists()
    Store(imported, 500)
    Store(imported).audit()


def test_lifecycle_refuses_lost_ledger_before_spawning(tmp_path, monkeypatch):
    from bcc.lifecycle import Lifecycle
    import subprocess
    repo = Repository(tmp_path / "data")
    bot = repo.create(poly_config())
    Store(bot, 500)
    (bot / "paper.sqlite").unlink()
    def must_not_spawn(*args, **kwargs):
        raise AssertionError("worker should not be spawned")
    monkeypatch.setattr(subprocess, "Popen", must_not_spawn)
    with pytest.raises(AccountingError, match="PAPER-счёт"):
        Lifecycle(repo).start(bot)
    assert not (bot / "paper.sqlite").exists()


def test_read_missing_ledger_does_not_create_sqlite(tmp_path):
    store = Store(tmp_path)
    with pytest.raises(Exception):
        store.read()
    assert not store.path.exists()


def test_legacy_started_bot_without_new_marker_cannot_reset(tmp_path):
    repo = Repository(tmp_path / "data")
    bot = repo.create(poly_config())
    # Old installations had no account marker but did persist process.json.
    (bot / "process.json").write_text('{"status":"STOPPED"}', encoding="utf-8")
    with pytest.raises(AccountingError, match="Существующий"):
        Store(bot, 500)
    assert not (bot / "paper.sqlite").exists()


def test_export_second_missing_account_preserves_old_zip(tmp_path):
    repo = Repository(tmp_path / "data")
    bots = [repo.create(poly_config()) for _ in range(2)]
    for bot in bots:
        Store(bot, 500)
    (bots[1] / "paper.sqlite").unlink()
    target = tmp_path / "backup.zip"
    target.write_bytes(b"retain original")
    with pytest.raises(AccountingError):
        export_bots(repo, bots, target, history=True)
    assert target.read_bytes() == b"retain original"
    assert not list(tmp_path.glob(".bcc-export-*"))


def test_import_detects_original_running_without_untrusted_paths(tmp_path):
    repo = Repository(tmp_path / "data")
    original = repo.create(poly_config())
    archive = tmp_path / "same_machine.zip"
    export_bots(repo, [original], archive, history=True)
    with BotLock(original), pytest.raises(BotBusy):
        import_bots(repo, archive, history=True)
    assert len(repo.list()) == 1
    restored = import_bots(repo, archive, history=True)
    assert len(restored) == 1
