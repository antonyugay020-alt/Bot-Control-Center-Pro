"""Shared allocation limits use committed accounts across actual processes."""

import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from bcc.config import defaults
from bcc.shared_risk import (
    DEFAULT_POLICY,
    allocation_reason,
    exposure,
    load_policy,
    portfolio_snapshot,
    risk_transaction,
    save_policy,
    validate_policy,
)
from bcc.storage import BotBusy, BotLock, Repository, Store, atomic_json
from bcc.worker import process_polymarket
from test_strategies import PolySource
from test_process import wait_for


def bot(repo, name):
    cfg = defaults("POLYMARKET")
    cfg.update(name=name, symbols=["BTC"])
    cfg["polymarket"]["durations"] = [300]
    folder = repo.create(cfg)
    return cfg, folder, Store(folder, cfg["capital"])


def policy(root, **changes):
    atomic_json(root / "shared_risk.json", {**DEFAULT_POLICY, **changes})


def test_default_does_not_introduce_position_limit(tmp_path):
    repo = Repository(tmp_path)
    _, _, store = bot(repo, "No limit")
    assert load_policy(repo.root) == DEFAULT_POLICY
    with risk_transaction(store) as (state, events):
        assert not state.get("_shared_risk")
        assert not allocation_reason(state, 1e8, slots=100_000)


@pytest.mark.parametrize(
    "limit,match",
    [({"max_positions": 1}, "позиций"), ({"max_committed_usdt": 40}, "обеспечения")],
)
def test_stopped_holdings_block_new_exposure_without_reset(tmp_path, limit, match):
    repo = Repository(tmp_path)
    cfg, first, a = bot(repo, "Stopped holdings")
    _, _, b = bot(repo, "New bot")
    source = PolySource()
    policy(repo.root, **limit)
    with risk_transaction(a) as (state, events):
        process_polymarket(cfg, state, events, source, source.now)
    saved = a.read()
    with risk_transaction(b) as (state, events):
        process_polymarket(cfg, state, events, source, source.now)
        assert not events and not state["positions"]
        assert match in state["diagnostics"]["POLYMARKET"][0]["reason"]
    assert a.read() == saved
    assert "_shared_risk" not in b.read()
    assert portfolio_snapshot(repo.root) == (1, 25)
    a.audit()
    b.audit()


def test_busy_risk_lease_blocks_only_new_allocations(tmp_path):
    repo = Repository(tmp_path)
    _, _, store = bot(repo, "Busy")
    policy(repo.root, max_positions=1)
    lease_folder = repo.root / ".shared_risk"
    lease_folder.mkdir()
    with BotLock(lease_folder):
        with risk_transaction(store) as (state, events):
            assert "другая операция" in allocation_reason(state, 25)
    with risk_transaction(store) as (state, events):
        assert not allocation_reason(state, 25)


def test_corrupted_foreign_account_blocks_entries(tmp_path):
    repo = Repository(tmp_path)
    _, _, a = bot(repo, "Damaged")
    _, _, b = bot(repo, "Healthy")
    a.path.write_bytes(b"invalid database")
    policy(repo.root, max_positions=5)
    with risk_transaction(b) as (state, events):
        assert "не удалось проверить" in allocation_reason(state, 25)
    assert b.read()["cash"] == 500
    b.audit()


def test_pending_grid_reserves_slot_and_full_dca_margin():
    state = dict(
        positions={"a": dict(margin=2, order_margin=8)}, pending={"b": dict(margin=15)}
    )
    assert exposure(state) == (2, 25)
    state["_shared_risk"] = dict(
        policy=dict(max_positions=3, max_committed_usdt=40),
        other_slots=0,
        other_committed=0,
    )
    assert not allocation_reason(state, 15)
    assert "обеспечения" in allocation_reason(state, 15.1)
    assert "позиций" in allocation_reason(state, 1, slots=2)


@pytest.mark.parametrize(
    "patch",
    [
        {"max_positions": True},
        {"max_positions": -1},
        {"max_committed_usdt": float("nan")},
    ],
)
def test_invalid_policy_is_rejected(patch):
    with pytest.raises(ValueError):
        validate_policy({**DEFAULT_POLICY, **patch})


def test_policy_changes_require_stopped_workers(tmp_path):
    class Running:
        def status(self, folder):
            return "Работает"

    repo = Repository(tmp_path)
    bot(repo, "Running")
    with pytest.raises(BotBusy):
        save_policy(repo, Running(), DEFAULT_POLICY)
    assert not (repo.root / "shared_risk.json").exists()


CHILD = r"""
import json,sys,time
from pathlib import Path
from bcc.shared_risk import risk_transaction
from bcc.storage import Store, load_json
from bcc.worker import process_polymarket
from test_strategies import PolySource
folder=Path(sys.argv[1]); marker=Path(sys.argv[2]); release=Path(sys.argv[3])
cfg=load_json(folder/'config.json')
with risk_transaction(Store(folder)) as (state,events):
 marker.write_text('ready')
 deadline=time.monotonic()+15
 while not release.exists():
  if time.monotonic()>deadline: raise RuntimeError('release timed out')
  time.sleep(.02)
 source=PolySource()
 process_polymarket(cfg,state,events,source,source.now)
"""


def test_two_processes_cannot_reserve_the_same_last_slot(tmp_path):
    repo = Repository(tmp_path / "app")
    cfg, a, first = bot(repo, "Process one")
    _, b, second = bot(repo, "Process two")
    policy(repo.root, max_positions=1)
    runner = tmp_path / "allocation_child.py"
    runner.write_text(CHILD, encoding="utf-8")
    marker, release = tmp_path / "ready", tmp_path / "release"
    root = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(root), str(root / "tests")]),
    }
    log = (tmp_path / "console.log").open("w", encoding="utf-8")
    child = subprocess.Popen(
        [sys.executable, str(runner), str(a), str(marker), str(release)],
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        wait_for(marker.exists)
        source = PolySource()
        # First process owns the allocation lease but has not yet committed.
        with risk_transaction(second) as (state, events):
            process_polymarket(cfg, state, events, source, source.now)
            assert not state["positions"]
            assert "другая операция" in state["diagnostics"]["POLYMARKET"][0]["reason"]
        release.write_text("go", encoding="utf-8")
        assert child.wait(timeout=10) == 0
        with risk_transaction(second) as (state, events):
            process_polymarket(cfg, state, events, source, source.now)
            assert "лимит позиций" in state["diagnostics"]["POLYMARKET"][0]["reason"]
        assert len(first.read()["positions"]) + len(second.read()["positions"]) == 1
        first.audit()
        second.audit()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        log.close()


def test_veles_pending_grid_counts_towards_portfolio_limit_and_can_recover(tmp_path):
    from bcc.config import condition, group
    from bcc.strategies import Veles
    from test_core import attach, bars

    repo = Repository(tmp_path)
    cfg = defaults("VELES")
    cfg.update(name="Reserved VELES", symbols=["BTCUSDT"])
    cfg["sections"] = cfg["sections"][:1]
    cfg["sections"][0]["entry"] = group(condition("PRICE", 100, "cross_up", 1))
    cfg["sections"][0]["exit"] = group(condition("PRICE", 110, "cross_up", 1))
    cfg["settings"]["orders"].update(count=2, first_offset_pct=1)
    folder = repo.create(cfg)
    store = Store(folder, 500)
    now = int(time.time() * 1000)
    history = bars([99, 101], now // 60000 * 60000 - 180000)
    policy(repo.root, max_positions=1)
    engine = Veles()
    with risk_transaction(store) as (state, events):
        attach(state, price=101)
        engine.process(cfg, state, events, "BTCUSDT", history, 100.99, 101.01, now)
    before = Store(folder).read()
    assert exposure(before) == (1, 25)
    assert before["pending"] and not before["positions"]
    # A new bot cannot allocate the reserved grid's last portfolio slot.
    poly_cfg, _, other = bot(repo, "Blocked by pending")
    source = PolySource()
    with risk_transaction(other) as (state, events):
        process_polymarket(poly_cfg, state, events, source, source.now)
        assert "лимит позиций" in state["diagnostics"]["POLYMARKET"][0]["reason"]
        assert not events
    # Lowering limits on stopped accounts never strands an accepted grid.
    policy(repo.root, max_positions=1, max_committed_usdt=1)
    with risk_transaction(Store(folder)) as (state, events):
        attach(state, price=99)
        engine.process(cfg, state, events, "BTCUSDT", history, 98.99, 99.01, now + 1000)
    recovered = Store(folder).read()
    assert recovered["positions"] and not recovered["pending"]
    assert exposure(recovered)[0] == 1
    Store(folder).audit()


def test_worker_crash_releases_allocation_lease_without_ghost_reservation(tmp_path):
    repo = Repository(tmp_path / "app")
    cfg, a, first = bot(repo, "Crashing allocator")
    _, _, second = bot(repo, "Recovery allocator")
    policy(repo.root, max_positions=1)
    runner = tmp_path / "crash_allocator.py"
    runner.write_text(r"""
import sys,time
from pathlib import Path
from bcc.shared_risk import risk_transaction
from bcc.storage import Store,load_json
from bcc.worker import process_polymarket
from test_strategies import PolySource
folder,marker=map(Path,sys.argv[1:])
with risk_transaction(Store(folder)) as (state,events):
 source=PolySource()
 process_polymarket(load_json(folder/'config.json'),state,events,source,source.now)
 assert state['positions'] and events
 marker.write_text('allocated but not committed')
 time.sleep(20)
""", encoding="utf-8")
    marker = tmp_path / "allocated"
    root = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(root), str(root / "tests")]),
    }
    with (tmp_path / "console.log").open("w", encoding="utf-8") as log:
        child = subprocess.Popen(
            [sys.executable, str(runner), str(a), str(marker)],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            wait_for(marker.exists)
            child.kill()
            child.wait(timeout=7)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
    first.audit()
    assert not first.read()["positions"] and not first.history()
    with risk_transaction(second) as (state, events):
        source = PolySource()
        process_polymarket(cfg, state, events, source, source.now)
        assert state["positions"] and events
    second.audit()
    assert portfolio_snapshot(repo.root)[0] == 1
