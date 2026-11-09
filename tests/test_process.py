"""Actual OS processes, persisted ledgers and controller stop/restart/force-stop."""

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from bcc.config import condition, defaults, group
from bcc.lifecycle import Lifecycle
from bcc.launch_claim import issue_launch, register_launcher
from bcc.storage import BotBusy, BotLock, Repository, Store, atomic_json

ROOT = Path(__file__).resolve().parents[1]
RUNNER = """
import json,time
from pathlib import Path
import os
print('REPLAY_START pid=%s parent=%s' % (os.getpid(), os.getppid()), flush=True)
import bcc.worker as worker
from bcc.market_clock import market_now, observed_quote
class Session:
 def close(self):pass
class ReplayPublicData:
 def __init__(self): self.session=Session()
 def candles(self,symbol,required):
  path=Path(__import__('sys').argv[__import__('sys').argv.index('--folder')+1])
  self.frame=json.loads((path/'feed.json').read_text(encoding='utf-8'));return self.frame['bars']
 def orderbook(self,symbol):
  p=self.frame['price'];stamp=market_now();return dict(at=stamp,bids=[[p-.01,10000]],asks=[[p+.01,10000]],qty_step=.0001,min_qty=.0001,min_notional=.01,tick_size=.01,
   margin_data=dict(symbol=symbol,quote=observed_quote(dict(at=stamp,bid=p,ask=p)),risk_at=stamp,
    tiers=[dict(id=1,limit=1e12,initial_rate=.01,maintenance_rate=.005,deduction=0,max_leverage=100)]))
 def funding(self,symbol):return []
worker.PublicData=ReplayPublicData
worker.main()
"""


def wait_for(predicate, seconds=7):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(0.05)
    raise AssertionError("Timed out waiting for process/state")


def wait_for_position_count(folder, child, expected, seconds=25):
    """Wait for a real child worker and expose why a trading cycle stalls.

    Windows CI can run significantly slower than a local process replay. A
    fixed seven-second assertion silently loses the worker evidence and can
    misclassify a scheduling delay as a failed RSI signal. This still fails
    closed: neither a dead child nor an incorrect position count can pass.
    """
    deadline = time.monotonic() + seconds
    latest = {}
    while time.monotonic() < deadline:
        if child.poll() is not None:
            break
        try:
            latest = Store(folder).read(include_charts=False)
            if len(latest.get("positions", {})) == expected:
                return latest
        except (OSError, ValueError, KeyError) as exc:
            latest = {"state_read_error": f"{type(exc).__name__}: {exc}"}
        time.sleep(0.075)
    try:
        latest = Store(folder).read(include_charts=False)
    except (OSError, ValueError, KeyError) as exc:
        latest = {"state_read_error": f"{type(exc).__name__}: {exc}"}
    try:
        process = json.loads((folder / "process.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        process = {}
    def log_tail(filename):
        try:
            return (folder / filename).read_text(encoding="utf-8", errors="replace")[-4000:]
        except OSError:
            return "<log unavailable>"
    raise AssertionError(
        "Real PAPER worker did not reach the required position count: "
        f"expected={expected}, actual={len(latest.get('positions', {}))}, "
        f"exit_code={child.poll()}, worker_status={process.get('status')}, "
        f"last_error={process.get('last_error')}, "
        f"diagnostics={latest.get('diagnostics')}, "
        f"positions={list(latest.get('positions', {}))}, "
        f"pending={list(latest.get('pending', {}))}\n"
        f"engine.log:\n{log_tail('engine.log')}\n"
        f"test-console.log:\n{log_tail('test-console.log')}"
    )


def setup_bot(repo, runner, name="Process"):
    cfg = defaults()
    cfg["name"] = name
    cfg["symbols"] = ["BTCUSDT"]
    cfg["sections"] = cfg["sections"][:1]
    cfg["sections"][0]["entry"] = group(condition("PRICE", 100, "cross_up", 1))
    cfg["sections"][0]["exit"] = group(condition("PRICE", 110, "cross_up", 1))
    cfg["settings"]["orders"]["count"] = 1
    folder = repo.create(cfg)
    base = int(time.time()) // 60 * 60000 - 180000
    frame = {
        "price": 101,
        "bars": [
            dict(t=base + i * 60000, open=x, high=x + 1, low=x - 1, close=x, volume=10)
            for i, x in enumerate([99, 101])
        ],
    }
    atomic_json(folder / "feed.json", frame)
    return folder, frame


def launch(life, folder, runner, *, accept_start=None):
    log = (folder / "test-console.log").open("ab")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    token = uuid.uuid4().hex
    try:
        # Replay workers still use the real controller authorization handshake.
        # Directly spawning with an arbitrary token must never work in release.
        with BotLock(folder):
            issue_launch(folder, token)
            try:
                if accept_start is not None:
                    accept_start()
                proc = subprocess.Popen(
                    [sys.executable, str(runner), "--folder", str(folder),
                     "--token", token],
                    env=env, stdout=log, stderr=subprocess.STDOUT,
                )
                register_launcher(folder, token, proc.pid)
            except BaseException:
                from bcc.launch_claim import claim_path
                claim_path(folder).unlink(missing_ok=True)
                if "proc" in locals():
                    proc.kill()
                    proc.wait(timeout=5)
                raise
    finally:
        log.close()
    life.children[str(folder)] = proc
    life.tokens[str(folder)] = token

    # Startup awaits the issued child and its authenticated snapshot. The
    # independent OS identity path is also exercised in the dedicated test,
    # including when procfs and Popen use different PID namespaces.
    def ready():
        if proc.poll() is not None:
            raise RuntimeError(f"Worker launcher exited with code {proc.returncode}")
        snapshot = life.active_snapshot(folder)
        if not isinstance(snapshot, dict) or snapshot.get("status") != "RUNNING":
            return None
        # Owned Popen + process.json is an early launch handshake, not the
        # independent OS identity that other controllers must trust after a
        # Qt/guardian restart. Windows may expose the child command line a
        # little later than the worker publishes its process.json. Never let
        # the fast owned-child path stand in for independently verified PID,
        # birth time, token and folder.
        independent = life.process(folder)
        if independent is None or independent.pid != snapshot["pid"]:
            return None
        if proc.poll() is not None:
            raise RuntimeError("Worker exited during independent identity confirmation")
        return snapshot

    try:
        wait_for(ready, seconds=20)
    except BaseException as exc:
        status = None
        try:
            status = json.loads((folder / "process.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        atomic_json(
            folder / "test_launch.json",
            {
                "launcher_pid": proc.pid,
                "launcher_exit_code": proc.poll(),
                "worker_pid": (status or {}).get("pid"),
                "worker_parent_pid": (status or {}).get("parent_pid"),
                "error": str(exc),
            },
        )
        if proc.poll() is None:
            life.stop(folder, force=True)
            proc.wait(timeout=7)
        detail = (folder / "test-console.log").read_text(
            encoding="utf-8", errors="replace"
        )[-12000:]
        raise AssertionError(
            f"Worker startup failed; launcher PID={proc.pid}, worker PID={(status or {}).get('pid')}, exit={proc.poll()}\n{detail}"
        ) from exc
    # This evidence is recorded only after the actual independent
    # verification passed; a transient fast-path observation cannot mark
    # the Windows acceptance worker as confirmed.
    confirmed = life.active_snapshot(folder)
    atomic_json(
        folder / "test_launch.json",
        {
            "launcher_pid": proc.pid,
            "worker_pid": confirmed["pid"],
            "worker_parent_pid": confirmed.get("parent_pid"),
            "independently_verified": True,
        },
    )
    return proc


def test_process_stop_restart_force_and_recovery(tmp_path):
    repo = Repository(tmp_path / "app")
    life = Lifecycle(repo)
    runner = tmp_path / "replay_worker.py"
    runner.write_text(RUNNER, encoding="utf-8")
    folder, frame = setup_bot(repo, runner)
    proc = launch(life, folder, runner)
    try:
        wait_for(lambda: Store(folder).read()["positions"])
        assert life.status(folder) == "Работает"
        assert life.active_snapshot(folder)["token"] == life.tokens[str(folder)]
        before = Store(folder).read()
        history = Store(folder).history()
        with pytest.raises(BotBusy):
            repo.copy(folder, history=True)
        life.stop(folder)
        proc.wait(timeout=7)
        from copy import deepcopy
        refreshed = deepcopy(Store(folder).read()["positions"])
        expected = deepcopy(before["positions"])
        for key in expected:
            assert refreshed[key]["margin_evidence"].pop("mark_at") >= expected[key]["margin_evidence"].pop("mark_at")
        assert refreshed == expected
        proc = launch(life, folder, runner)
        assert proc.poll() is None
        assert len(Store(folder).history()) == len(history)
        life.stop(folder, force=True)
        proc.wait(timeout=7)
        Store(folder).audit()
        from copy import deepcopy
        refreshed = deepcopy(Store(folder).read()["positions"])
        expected = deepcopy(before["positions"])
        for key in expected:
            assert refreshed[key]["margin_evidence"].pop("mark_at") >= expected[key]["margin_evidence"].pop("mark_at")
        assert refreshed == expected
        # Publish the complete immutable replay frame *before* spawning the
        # worker. The old ordering could let a fast child consume the prior
        # frame and complete its one cycle before the fixture updated it.
        last = frame["bars"][-1]
        frame["bars"].append(
            dict(t=last["t"] + 60000, open=111, high=112, low=110, close=111, volume=10)
        )
        frame["price"] = 111
        atomic_json(folder / "feed.json", frame)
        proc = launch(life, folder, runner)
        # A restarted worker may be scheduled behind real OS/PAPER processes
        # during the full suite. Assert the actual independent worker's ledger
        # transition with an explicit liveness check and useful diagnostics.
        wait_for_position_count(folder, proc, 0, seconds=25)
        life.stop(folder)
        proc.wait(timeout=7)
        Store(folder).audit()
        assert [e["action"] for e in reversed(Store(folder).history())] == [
            "OPEN",
            "CLOSE",
        ]
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_parallel_bots_do_not_share_positions_or_balances(tmp_path):
    repo = Repository(tmp_path / "app")
    life = Lifecycle(repo)
    runner = tmp_path / "replay_worker.py"
    runner.write_text(RUNNER, encoding="utf-8")
    a, _ = setup_bot(repo, runner, "A")
    b, _ = setup_bot(repo, runner, "B")
    children = []
    try:
        for folder in (a, b):
            children.append(launch(life, folder, runner))
        for folder in (a, b):
            wait_for(lambda f=folder: Store(f).read()["positions"])
        assert a != b and Store(a).path != Store(b).path
        for folder in (a, b):
            life.stop(folder)
        for child in children:
            child.wait(timeout=7)
        assert len(Store(a).history()) == len(Store(b).history()) == 1
        for folder in (a, b):
            Store(folder).audit()
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()


def test_independent_process_identity_with_native_procfs(tmp_path):
    repo = Repository(tmp_path / "app")
    life = Lifecycle(repo)
    runner = tmp_path / "replay_worker.py"
    runner.write_text(RUNNER, encoding="utf-8")
    folder, _ = setup_bot(repo, runner)
    proc = launch(life, folder, runner)
    try:
        assert life.process(folder).pid == life.active_snapshot(folder)["pid"]
        life.stop(folder)
        proc.wait(timeout=7)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_launch_never_reports_ready_before_independent_os_identity(tmp_path, monkeypatch):
    """A live launcher/JSON alone is not proof that Windows sees our worker."""
    repo = Repository(tmp_path / "app")
    life = Lifecycle(repo)
    runner = tmp_path / "replay_worker.py"
    runner.write_text(RUNNER, encoding="utf-8")
    folder, _ = setup_bot(repo, runner)
    real_process = life.process
    probes = []

    def delayed_independent_identity(path):
        probes.append(path)
        # Model the short native-Windows command-line visibility delay.
        if len(probes) <= 4:
            return None
        return real_process(path)

    monkeypatch.setattr(life, "process", delayed_independent_identity)
    proc = launch(life, folder, runner)
    try:
        assert len(probes) >= 5, "Fast owned-child snapshot bypassed OS verification"
        evidence = json.loads((folder / "test_launch.json").read_text(encoding="utf-8"))
        assert evidence["independently_verified"] is True
        assert evidence["worker_pid"] == life.active_snapshot(folder)["pid"]
        assert proc.poll() is None
    finally:
        if proc.poll() is None:
            life.stop(folder)
            proc.wait(timeout=7)


def test_worker_stops_automatically_after_deal_when_configured(tmp_path):
    repo = Repository(tmp_path / "app")
    life = Lifecycle(repo)
    runner = tmp_path / "replay_worker.py"
    runner.write_text(RUNNER, encoding="utf-8")
    folder, frame = setup_bot(repo, runner)
    cfg, digest = repo.load(folder)
    cfg["stop_after_deals"] = True
    repo.save(folder, cfg, digest)
    child = launch(life, folder, runner)
    try:
        wait_for(lambda: Store(folder).read()["positions"])
        last = frame["bars"][-1]
        frame["bars"].append(
            dict(t=last["t"] + 60000, open=111, high=112, low=110, close=111, volume=10)
        )
        frame["price"] = 111
        atomic_json(folder / "feed.json", frame)
        child.wait(timeout=7)
        assert child.returncode == 0
        assert not Store(folder).read()["positions"]
        assert Store(folder).read()["strategy"]["draining"]
        assert life.status(folder) == "Остановлен"
        Store(folder).audit()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=7)


def test_stop_during_startup_is_not_erased_and_old_request_does_not_block_restart(
    tmp_path, monkeypatch
):
    repo = Repository(tmp_path / "app")
    life = Lifecycle(repo)
    runner = tmp_path / "slow_start_worker.py"
    runner.write_text(
        RUNNER.replace(
            "worker.main()",
            """
class SlowStore(worker.Store):
 def audit(self):
  super().audit()
  folder=self.path.parent
  (folder/'startup.ready').touch()
  deadline=time.monotonic()+10
  while not (folder/'startup.release').exists():
   if time.monotonic()>deadline: raise RuntimeError('Startup barrier timeout')
   time.sleep(.01)
worker.Store=SlowStore
worker.main()
""",
        )
    , encoding="utf-8")
    folder, _ = setup_bot(repo, runner)
    atomic_json(folder / "stop.request", {"id": "old_stopped_run"})
    popen = subprocess.Popen

    def replay_start(args, **kwargs):
        return popen([args[0], str(runner), *args[3:]], **kwargs)

    monkeypatch.setattr("bcc.lifecycle.subprocess.Popen", replay_start)
    life.start(folder)
    child = life.children[str(folder)]
    try:
        wait_for(lambda: (folder / "startup.ready").exists())
        assert not (folder / "stop.request").exists()
        life.stop(folder)
        assert (folder / "stop.request").exists()
        (folder / "startup.release").touch()
        child.wait(timeout=7)
        assert child.returncode == 0
        assert not Store(folder).read()["positions"]
        assert not Store(folder).history()
        assert life.status(folder) == "Остановлен"
        Store(folder).audit()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=7)


def test_multicoin_rsi_replay_process_reaches_both_positions(tmp_path):
    """Same replayed RSI signal in both symbols reaches durable PAPER state.

    Unlike the Qt wizard E2E, this isolates worker scheduling and accounting;
    a later Windows E2E failure can be attributed to its integration layer.
    """
    import math
    from bcc.indicators import evaluate

    cfg = defaults()
    cfg["name"] = "Replay two RSI coins"
    cfg["sections"] = cfg["sections"][:1]
    cfg["sections"][0]["exit"]["conditions"][0]["value"] = 70
    cfg["settings"]["orders"]["count"] = 1
    repo = Repository(tmp_path / "app")
    folder = repo.create(cfg)
    life = Lifecycle(repo)
    base = int(time.time()) // 120 * 120000 - 148 * 60000
    prices = [100 + 6 * math.sin(i / 12) for i in range(148)]
    bars = [
        dict(t=base + i * 60000, open=p, high=p + 0.2, low=p - 0.2,
             close=p, volume=100 + i % 4)
        for i, p in enumerate(prices)
    ]
    frame = {"bars": bars[:140], "price": prices[139],
             "now": (base + 140 * 60000) / 1000}
    assert evaluate(cfg["sections"][0]["entry"], frame["bars"],
                    int(frame["now"] * 1000), {}).matched
    atomic_json(folder / "feed.json", frame)
    clock = """
# Keep the synthetic market epoch and monotonic receipt clock advancing
# together. Freezing time.time while monotonic runs makes genuine stale-quote
# protections correctly reject the second symbol after the first position.
_feed_path = Path(__import__('sys').argv[__import__('sys').argv.index('--folder')+1])
_epoch = json.loads((_feed_path/'feed.json').read_text(encoding='utf-8'))['now']
_anchor = time.monotonic()
def replay_now():
 return _epoch + (time.monotonic() - _anchor)
time.time=replay_now
"""
    runner = tmp_path / "rsi_replay_worker.py"
    runner.write_text(RUNNER.replace("worker.main()", clock + "\nworker.main()"),
                      encoding="utf-8")
    child = launch(life, folder, runner)
    try:
        state = wait_for_position_count(folder, child, 2)
        assert {p["symbol"] for p in state["positions"].values()} == {
            "BTCUSDT", "ETHUSDT"
        }
        assert {e["action"] for e in Store(folder).history()} == {"OPEN"}
        life.stop(folder)
        child.wait(timeout=10)
        assert len(Store(folder).read()["positions"]) == 2
        Store(folder).audit()
    finally:
        if child.poll() is None:
            life.stop(folder, force=True)
            child.wait(timeout=10)
