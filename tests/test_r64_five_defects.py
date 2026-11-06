"""Acceptance guards for five previously detected R6.4 defects.

All tests use disposable directories, fake API replies and PAPER state only.
"""
import json
import math
import shutil
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from bcc.launch_claim import launcher_alive
from bcc.lifecycle import Lifecycle
from bcc.market import DataUnavailable, PublicData
from bcc.scheduler import MarketScheduler
from bcc.storage import BotBusy, BotLock, Repository, Store, atomic_json, initial_state
from bcc.transfer import export_bots, import_bots
from test_core import config


def _batch(tmp_path, count=2):
    repo = Repository(tmp_path / "app")
    originals = [repo.create(config()) for _ in range(count)]
    for i, original in enumerate(originals):
        Store(original, 500)
        (original / "engine.log").write_text(f"Original history {i}", encoding="utf-8")
    target = tmp_path / "export.zip"
    export_bots(repo, originals, target)
    return repo, target, originals


@pytest.mark.parametrize("workers", [1, 2, 3, 8])
def test_continuous_position_priority_cannot_starve_new_symbols(workers):
    fetched = []

    class Fake:
        def candles(self, symbol, count):
            fetched.append(symbol)
            return []

        def orderbook(self, symbol):
            return {}

        def funding(self, symbol):
            return []

    coins = [f"C{i}" for i in range(80)]
    state = initial_state(500)
    state["positions"] = {
        f"{i}:main": {"symbol": coins[i], "opened": 1}
        for i in range(20)
    }
    scheduler = MarketScheduler(Fake(), {"symbols": coins},
                                lambda *_: 1, workers=workers, period=0)
    try:
        deadline = time.monotonic() + 3
        while (len(set(fetched[20:])) < 1 or len(set(fetched[:20])) < 1) and time.monotonic() < deadline:
            # Count actual symbols, not positions in a time-dependent log.
            scheduler.collect(state, timeout=.05)
            if any(s not in coins[:20] for s in fetched) and any(s in coins[:20] for s in fetched):
                break
        assert any(s not in coins[:20] for s in fetched), (workers, fetched)
        assert any(s in coins[:20] for s in fetched), (workers, fetched)
        # Priority must still continue recurring while discovering coins.
        for _ in range(70):
            scheduler.collect(state, timeout=.05)
        assert len({s for s in fetched if s not in coins[:20]}) >= 10
        assert len([s for s in fetched if s in coins[:20]]) >= 10
    finally:
        scheduler.close()


@pytest.mark.parametrize("snapshot", [
    [], "garbage", {}, {"pid": "100", "created": 10, "token": "A"},
    {"pid": True, "created": 10, "token": "A"},
    {"pid": 100, "created": None, "token": "A"},
    {"pid": 100, "created": float("nan"), "token": "A"},
    {"pid": 100, "created": float("inf"), "token": "A"},
    {"pid": 100, "created": 10, "token": {}},
    {"pid": 100, "created": 10, "token": "A"},
])
def test_corrupt_process_record_cannot_confirm_foreign_pid(tmp_path, monkeypatch, snapshot):
    repo = Repository(tmp_path)
    folder = repo.create(config())
    (folder / "process.json").write_text(json.dumps(snapshot), encoding="utf8")
    calls = []

    def process(pid):
        calls.append(pid)
        return SimpleNamespace(create_time=lambda: 10,
                               cmdline=lambda: ["python", "--token", "A", "--folder", str(folder)],
                               status=lambda: "running")

    monkeypatch.setattr("bcc.lifecycle.psutil.Process", process)
    result = Lifecycle(repo).process(folder)
    if snapshot == {"pid": 100, "created": 10, "token": "A"}:
        assert result is not None
    else:
        assert result is None
        assert not calls


@pytest.mark.parametrize("args", [
    [], ["python", "--token"], ["python", "--folder"],
    ["python", "--token", "X", "--folder"],
    ["python", "--token", "X", "--folder", "not-the-bot"],
    ["python", "--token", "X", "--token", "X", "--folder", "ignored"],
    ["python", "--folder", "ignored", "--token", "X", "--folder", "ignored"],
])
def test_malformed_cmdline_never_verifies_other_process(tmp_path, monkeypatch, args):
    repo = Repository(tmp_path)
    folder = repo.create(config())
    atomic_json(folder / "process.json", {"pid": 100, "created": 10, "token": "X"})
    monkeypatch.setattr("bcc.lifecycle.psutil.Process", lambda pid:
                        SimpleNamespace(create_time=lambda: 10,
                                        cmdline=lambda: args,
                                        status=lambda: "running"))
    assert Lifecycle(repo).process(folder) is None


def test_access_denied_cannot_confirm_a_stale_launcher(monkeypatch):
    from bcc import process_os as psutil
    monkeypatch.setattr("bcc.launch_claim.psutil.Process", lambda pid: (_ for _ in ()).throw(psutil.AccessDenied(pid)))
    monkeypatch.setattr("bcc.launch_claim.psutil.pid_exists", lambda pid: True)
    assert launcher_alive({"pid": 123, "created": 1}) is False


@pytest.mark.parametrize("rows", [
    [{"symbol": "ETHUSDT", "bid1Price": "1", "ask1Price": "2"}],
    [{"bid1Price": "1", "ask1Price": "2"}],
    [],
    [{"symbol": "BTCUSDT", "bid1Price": "1", "ask1Price": "2"},
     {"symbol": "ETHUSDT", "bid1Price": "1", "ask1Price": "2"}],
    [None],
])
def test_bybit_quote_cannot_cross_trading_pairs(monkeypatch, rows):
    monkeypatch.setattr("bcc.market.clock.ensure", lambda: None)
    monkeypatch.setattr("bcc.market.market_now", lambda: 10)
    source = PublicData()
    monkeypatch.setattr(source, "bybit", lambda *a: {"result": {"list": rows}, "time": 10000})
    try:
        with pytest.raises(DataUnavailable):
            source.quote("BTCUSDT")
    finally:
        source.close()


def test_bybit_matching_single_ticker_is_allowed(monkeypatch):
    monkeypatch.setattr("bcc.market.clock.ensure", lambda: None)
    monkeypatch.setattr("bcc.market.market_now", lambda: 10)
    source = PublicData()
    monkeypatch.setattr(source, "bybit", lambda *a: {
        "result": {"list": [{"symbol": "BTCUSDT", "bid1Price": "1", "ask1Price": "2"}]},
        "time": 10000,
    })
    try:
        assert source.quote("BTCUSDT") == (1, 2, 10)
    finally:
        source.close()


def test_batch_is_visible_atomically_after_last_publish(tmp_path, monkeypatch):
    repo, target, originals = _batch(tmp_path)
    before = {path for path, _ in repo.list()}
    real_publish = repo.publish
    seen = []

    def partial(staged):
        published = real_publish(staged)
        seen.append(published)
        assert {path for path, _ in repo.list()} == before
        with pytest.raises((BotBusy, ValueError)):
            with BotLock(published):
                pass
        return published

    monkeypatch.setattr(repo, "publish", partial)
    result = import_bots(repo, target)
    assert len(result) == 2
    assert set(result) == set(seen)
    assert {path for path, _ in repo.list()} == before | set(result)
    for path in result:
        Store(path).audit()


def test_publish_failure_and_failed_cleanup_keep_paper_hidden(tmp_path, monkeypatch):
    repo, target, originals = _batch(tmp_path)
    before = {path for path, _ in repo.list()}
    publish = repo.publish
    real_rename = Path.rename
    real_rmtree = shutil.rmtree
    created = []

    def break_after_publish(staged):
        final = publish(staged)
        created.append(final)
        if len(created) == 2:
            raise OSError("second publish interrupted")
        return final

    def reject_quarantine(self, target):
        if self in created and Path(target).name.startswith(".quarantined-"):
            raise PermissionError("cannot quarantine")
        return real_rename(self, target)

    def reject_cleanup(path, *args, **kwargs):
        if path in created:
            raise PermissionError("cannot clean")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(repo, "publish", break_after_publish)
    monkeypatch.setattr(Path, "rename", reject_quarantine)
    monkeypatch.setattr(shutil, "rmtree", reject_cleanup)
    with pytest.raises(OSError, match="second publish interrupted"):
        import_bots(repo, target)
    assert {path for path, _ in repo.list()} == before
    for path in created:
        assert (path / "paper.sqlite").exists()
        assert (path / ".import-batch").exists()
        with pytest.raises((BotBusy, ValueError)):
            with BotLock(path):
                pass
    for path in originals:
        Store(path).audit()


def test_dashboard_listing_has_single_commit_snapshot(tmp_path, monkeypatch):
    repo, target, originals = _batch(tmp_path)
    starting = {p for p, _ in repo.list()}
    prepared = [repo.prepare(config()) for _ in range(2)]
    batch = "a" * 32
    imported = []
    for staged in prepared:
        (staged / ".import-batch").write_text(batch, encoding="ascii")
        imported.append(repo.publish(staged))
    assert {p for p, _ in repo.list()} == starting

    actual_glob = Path.glob
    committed = []

    def midway_commit(path, pattern, **kwargs):
        if path == repo.bots and pattern == "*/*/config.json" and not committed:
            commits = repo.bots / ".import-commits"
            commits.mkdir(exist_ok=True)
            (commits / batch).write_text(batch, encoding="ascii")
            committed.append(True)
        return actual_glob(path, pattern, **kwargs)

    # This test deliberately exercises the monitoring hook while pathlib is
    # patched, even when no external QA collector environment was configured.
    monkeypatch.setenv("BCC_TEST_ARTIFACTS", str(tmp_path / "qa_artifacts"))
    monkeypatch.setattr(Path, "glob", midway_commit)
    assert {p for p, _ in repo.list()} == starting
    assert {p for p, _ in repo.list()} == starting | set(imported)
