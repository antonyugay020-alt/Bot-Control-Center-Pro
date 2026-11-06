import json
import zipfile

import pytest
from test_core import config

from bcc.storage import BotBusy, BotLock, Repository, Store
from bcc.transfer import export_bots, import_bots


def test_transfer_full_and_parameters_only_are_independent(tmp_path):
    repo = Repository(tmp_path / "app")
    original = repo.create(config())
    Store(original, 500)
    (original / "engine.log").write_text(
        "Сигнал → PAPER-исполнение\n", encoding="utf-8"
    )
    target = tmp_path / "all.zip"
    export_bots(repo, [original], target, history=True)
    full = import_bots(repo, target, history=True)[0]
    clean = import_bots(repo, target, history=False)[0]
    assert (full / "engine.log").read_bytes() == (original / "engine.log").read_bytes()
    Store(full).audit()
    assert not (clean / "paper.sqlite").exists()
    assert not (clean / "engine.log").exists()
    assert len({original, full, clean}) == 3


def test_failed_export_preserves_existing_file(tmp_path):
    repo = Repository(tmp_path / "app")
    first = repo.create(config())
    second = repo.create(config())
    target = tmp_path / "backup.zip"
    target.write_bytes(b"existing backup")
    with BotLock(second), pytest.raises(BotBusy):
        export_bots(repo, [first, second], target)
    assert target.read_bytes() == b"existing backup"
    assert not list(tmp_path.glob(".bcc-export-*"))


def test_corrupted_imported_ledger_rolls_back_all_new_bots(tmp_path):
    repo = Repository(tmp_path / "app")
    folder = repo.create(config())
    store = Store(folder, 500)
    state = store.read()
    state["cash"] -= 1
    with store.connect() as db:
        # Simulate a modified state document without a matching cash event.
        db.execute("UPDATE state SET document=?", (json.dumps(state),))
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr(
            "manifest.json", json.dumps({"format": 1, "bots": [{"prefix": "bot_0"}]})
        )
        z.writestr("bot_0/config.json", json.dumps(config()))
        z.write(folder / "paper.sqlite", "bot_0/paper.sqlite")
    before = {p for p, _ in repo.list()}
    with pytest.raises(RuntimeError, match="Баланс"):
        import_bots(repo, archive)
    assert {p for p, _ in repo.list()} == before
