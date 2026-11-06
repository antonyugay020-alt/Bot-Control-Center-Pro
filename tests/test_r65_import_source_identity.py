"""A changed original config must never bypass a live-origin import interlock."""
import pytest

from bcc.config import defaults
from bcc.storage import BotBusy, BotLock, Repository
from bcc.transfer import export_bots, import_bots


def test_changed_original_config_does_not_bypass_running_origin_guard(tmp_path):
    repo = Repository(tmp_path / "profile")
    config = defaults("POLYMARKET")
    config["name"] = "Source bot"
    src = repo.create(config)
    archive = tmp_path / "before_change.zip"
    export_bots(repo, [src], archive, history=True)
    updated, digest = repo.load(src)
    updated["name"] = "Updated original settings after export"
    repo.save(src, updated, digest)
    with BotLock(src), pytest.raises(BotBusy):
        import_bots(repo, archive, history=True)
    assert len(repo.list()) == 1
    imported = import_bots(repo, archive, history=True)
    assert len(imported) == 1 and imported[0] != src
