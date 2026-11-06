"""Independent corrupt-state, concurrent-monitor and batch-import safety checks.

Every test operates only in a temporary PAPER workspace. No external requests.
"""
import json
import sqlite3
import zipfile

import pytest

from bcc.storage import AccountingError, Repository, Store
from bcc.lifecycle import Lifecycle
from bcc.transfer import import_bots
from test_core import config


@pytest.mark.parametrize("field", (
    "pending", "clocks", "marks", "diagnostics", "stats", "done", "daily", "strategy"
))
@pytest.mark.parametrize("reader", ("read", "audit", "transaction", "chart_symbols"))
def test_state_mapping_corruption_is_rejected_without_account_reset(tmp_path, field, reader):
    store = Store(tmp_path, 100)
    path = tmp_path / "paper.sqlite"
    with sqlite3.connect(path) as db:
        original = json.loads(db.execute("SELECT document FROM state WHERE id=1").fetchone()[0])
        original[field] = []
        damaged = json.dumps(original)
        db.execute("UPDATE state SET document=? WHERE id=1", (damaged,))
    with pytest.raises(AccountingError):
        if reader == "transaction":
            with store.transaction(include_charts=False):
                pass
        else:
            getattr(store, reader)()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT document FROM state WHERE id=1").fetchone()[0] == damaged
    assert (tmp_path / Store.MARKER).exists()


@pytest.mark.parametrize("bad", ([], "broken", 999, None))
def test_corrupt_pending_order_is_never_accepted(tmp_path, bad):
    store = Store(tmp_path, 50)
    with sqlite3.connect(store.path) as db:
        state = json.loads(db.execute("SELECT document FROM state WHERE id=1").fetchone()[0])
        state["pending"] = {"BTCUSDT:main": bad}
        db.execute("UPDATE state SET document=? WHERE id=1", (json.dumps(state),))
    with pytest.raises(AccountingError):
        store.audit()


@pytest.mark.parametrize("bad", ([], {}, "yes", 2, None))
def test_corrupt_chart_table_marker_blocks_paper_recovery(tmp_path, bad):
    store = Store(tmp_path, 100)
    with sqlite3.connect(store.path) as db:
        state = json.loads(db.execute("SELECT document FROM state WHERE id=1").fetchone()[0])
        state["_chart_table"] = bad
        db.execute("UPDATE state SET document=? WHERE id=1", (json.dumps(state),))
    with pytest.raises(AccountingError):
        store.audit()


@pytest.mark.parametrize("raw", ("null", "[]", "42", '"text"'))
def test_malformed_transfer_manifest_cannot_publish_bots(tmp_path, raw):
    repo = Repository(tmp_path / "workspace")
    source = tmp_path / "transfer.zip"
    with zipfile.ZipFile(source, "w") as z:
        z.writestr("manifest.json", raw)
    with pytest.raises(ValueError, match="формат"):
        import_bots(repo, source)
    assert repo.list() == []


def test_live_process_snapshot_replacement_cannot_crash_monitor(tmp_path, monkeypatch):
    repo = Repository(tmp_path)
    folder = repo.create(config())
    life = Lifecycle(repo)
    class FakeProcess:
        pid = 98124
        def create_time(self):
            return 123.25
    monkeypatch.setattr(life, "process", lambda folder: FakeProcess())
    monkeypatch.setattr("bcc.lifecycle.load_json", lambda path: (_ for _ in ()).throw(ValueError("raced")))
    assert life.status(folder) == "Занят другим процессом"


def test_valid_account_still_supports_concurrent_chartless_transaction(tmp_path):
    store = Store(tmp_path, 100)
    with store.transaction(include_charts=False) as (state, events):
        state["strategy"]["draining"] = True
        state["done"]["cycle:main"] = True
    Store(tmp_path).audit()
    assert Store(tmp_path).read(include_charts=False)["strategy"]["draining"] is True
