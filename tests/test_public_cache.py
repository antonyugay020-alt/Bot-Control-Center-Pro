"""Real SQLite contention stays inside the disposable public-data cache."""

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

from bcc.data_cache import CacheBusy, PublicCache
from bcc.market import DataUnavailable, PublicData

URL = "https://api.bybit.com/v5/market/orderbook"
HOST = "api.bybit.com"


@pytest.fixture(autouse=True)
def close_cache_connections(monkeypatch):
    instances = []
    original = PublicCache.__init__

    def create(self, *args, **kwargs):
        original(self, *args, **kwargs)
        instances.append(self)

    monkeypatch.setattr(PublicCache, "__init__", create)
    yield
    for cache in instances:
        cache.close()


def seeded(path):
    cache = PublicCache(path)
    key, owner, _ = cache.begin(URL, {"symbol": "BTCUSDT"}, HOST)
    cache.finish(key, owner, HOST, {"fresh": True}, ttl=30)
    return cache


def test_wal_hit_remains_readable_during_writer_and_miss_recovers(tmp_path):
    cache = seeded(tmp_path / "public.sqlite")
    blocker = sqlite3.connect(cache.path, isolation_level=None)
    try:
        assert blocker.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        blocker.execute("BEGIN IMMEDIATE")
        _, owner, value = cache.begin(URL, {"symbol": "BTCUSDT"}, HOST)
        assert owner is None and value == {"fresh": True}
        before = time.monotonic()
        with pytest.raises(CacheBusy, match="кэш"):
            cache.begin(URL, {"symbol": "ETHUSDT"}, HOST)
        assert time.monotonic() - before < 2  # No former 5-second snapshot stall.
        blocker.rollback()
        _, owner, _ = cache.begin(URL, {"symbol": "ETHUSDT"}, HOST)
        assert owner
    finally:
        blocker.close()


def test_locked_cache_prevents_unbudgeted_http_then_recovers(tmp_path):
    source = PublicData()
    source.shared = seeded(tmp_path / "public.sqlite")
    calls = []

    class Session:
        def get(self, *args, **kwargs):
            calls.append(args)
            return type(
                "Response",
                (),
                {
                    "status_code": 200,
                    "headers": {},
                    "raise_for_status": lambda s: None,
                    "json": lambda s: {"at": time.time()},
                },
            )()

    source._thread.session = Session()
    blocker = sqlite3.connect(source.shared.path, isolation_level=None)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        with pytest.raises(DataUnavailable, match="кэш"):
            source.get(URL, {"symbol": "ETHUSDT"})
        assert not calls
        blocker.rollback()
        assert source.get(URL, {"symbol": "ETHUSDT"})["at"] <= time.time()
        assert len(calls) == 1
    finally:
        blocker.close()
        source.close()


@pytest.mark.parametrize("http_error", [False, True])
def test_lock_after_http_does_not_crash_or_mask_transport_error(tmp_path, http_error):
    source = PublicData()
    source.shared = seeded(tmp_path / "public.sqlite")
    blocker = sqlite3.connect(source.shared.path, isolation_level=None)

    class Session:
        def get(self, *args, **kwargs):
            blocker.execute("BEGIN IMMEDIATE")
            if http_error:
                raise requests.ConnectionError("network unavailable")
            return type(
                "Response",
                (),
                {
                    "status_code": 200,
                    "headers": {},
                    "raise_for_status": lambda s: None,
                    "json": lambda s: {"payload": "new"},
                },
            )()

    source._thread.session = Session()
    try:
        if http_error:
            with pytest.raises(DataUnavailable, match="network unavailable"):
                source.get(URL, {"symbol": "ETHUSDT"})
            assert source.cooldowns[HOST] > time.time()
        else:
            # A successful HTTP response must not escape a failed SQLite
            # publication. Previously this silently returned uncommitted data.
            with pytest.raises(DataUnavailable, match="не подтверждён транзакцией"):
                source.get(URL, {"symbol": "ETHUSDT"})
        blocker.rollback()
        # Lease ownership and rate reservation were committed before HTTP.
        with source.shared.db() as db:
            assert (
                db.execute(
                    "SELECT count FROM request_budget WHERE host=?", (HOST,)
                ).fetchone()[0]
                >= 1
            )
    finally:
        blocker.close()
        source.close()


def test_cache_schema_lock_on_first_use_is_recoverable(tmp_path):
    path = tmp_path / "new.sqlite"
    blocker = sqlite3.connect(path, isolation_level=None)
    source = PublicData()
    try:
        blocker.execute("CREATE TABLE unrelated(x)")
        blocker.execute("BEGIN IMMEDIATE")
        source.shared = PublicCache(path)  # Construction does not block worker startup.
        with pytest.raises(DataUnavailable, match="кэш"):
            source.get(URL, {"symbol": "BTCUSDT"})
        blocker.rollback()
        assert source.shared.begin(URL, {}, HOST)[1]
    finally:
        blocker.close()
        source.close()


def test_single_flight_budget_and_cooldown_remain_atomic(tmp_path, monkeypatch):
    monkeypatch.setattr("bcc.data_cache.time.time", lambda: 1000)
    a = PublicCache(tmp_path / "public.sqlite")
    b = PublicCache(a.path)
    key, owner, _ = a.begin(URL, {"same": 1}, "api.binance.com")
    with pytest.raises(CacheBusy, match="другим ботом"):
        b.begin(URL, {"same": 1}, "api.binance.com")
    b.finish(key, "not-owner", "api.binance.com", {"wrong": 1}, ttl=30)
    with pytest.raises(CacheBusy):
        b.begin(URL, {"same": 1}, "api.binance.com")
    for i in range(9):
        a.begin(URL, {"unique": i}, "api.binance.com")
    with pytest.raises(CacheBusy, match="бюджет"):
        b.begin(URL, {"unique": 99}, "api.binance.com")
    a.finish(key, owner, "api.binance.com", cooldown=90)
    with pytest.raises(CacheBusy, match="Общий лимит"):
        b.begin(URL, {"after": 1}, "api.binance.com")


STRESS = r"""
import json,sys,time,os
from concurrent.futures import ThreadPoolExecutor
from bcc.data_cache import PublicCache,CacheBusy
cache=PublicCache(sys.argv[1]);worker=int(sys.argv[2])
payload={"bars":[[i,100,101,99,100.5,12345.6] for i in range(500)]}
def retry(call):
 deadline=time.monotonic()+8
 while True:
  try:return call()
  except CacheBusy:
   if time.monotonic()>deadline:raise
   time.sleep(.005)
def work(i):
 host="stress-%s-%s"%(worker,i//20);params={"symbol":i,"worker":worker}
 key,owner,_=retry(lambda:cache.begin("https://public.test/candles",params,host))
 assert owner
 retry(lambda:cache.finish(key,owner,host,payload,ttl=60))
 _,other,found=retry(lambda:cache.begin("https://public.test/candles",params,host))
 assert other is None and found==payload
 return 1
with ThreadPoolExecutor(max_workers=2) as pool:count=sum(pool.map(work,range(worker,813,4)))
cache.close()
print(json.dumps({"pid":os.getpid(),"written_and_read":count,"cache_stats":cache.stats}))
"""


def test_813_histories_across_four_processes_and_eight_threads(tmp_path):
    children = []
    try:
        for worker in range(4):
            children.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        STRESS,
                        str(tmp_path / "shared.sqlite"),
                        str(worker),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                )
            )
        evidence = []
        for child in children:
            out, err = child.communicate(timeout=45)
            assert child.returncode == 0, err
            evidence.append(json.loads(out))
        assert sum(row["written_and_read"] for row in evidence) == 813
        assert len({row["pid"] for row in evidence}) == 4
        assert all(row["cache_stats"]["connections"] == 1 for row in evidence)
        root = os.environ.get("BCC_TEST_ARTIFACTS")
        if root:
            target = Path(root) / "public_cache_stress.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(
                    {
                        "processes": evidence,
                        "threads_total": 8,
                        "histories_written_and_read": 813,
                        "bars_per_history": 500,
                    }
                ),
                encoding="utf-8",
            )
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)


def test_worker_loop_recovers_cache_lock_and_manages_existing_position(tmp_path):
    from test_core import bars, book, config
    from test_execution_runtime import open_position

    from bcc.runtime import run_loop
    from bcc.storage import Store
    from bcc.strategies import Veles

    cfg = config()
    cfg["symbols"] = ["BTCUSDT"]
    cfg["settings"]["orders"]["count"] = 1
    cfg["settings"]["profit"].update(mode="simple", pct=1)
    (tmp_path / "bot").mkdir()
    store = Store(tmp_path / "bot", 500)
    with store.transaction() as (state, events):
        open_position(state, events, cfg["settings"])
        state["positions"]["BTCUSDT:main"]["exit_signal"] = cfg["sections"][0]["exit"]
        state.pop("_books")

    class Session:
        def get(self, *args, **kwargs):
            return type(
                "Response",
                (),
                {
                    "status_code": 200,
                    "headers": {},
                    "raise_for_status": lambda s: None,
                    "json": lambda s: book(110),
                },
            )()

    class Source(PublicData):
        @property
        def session(self):
            return Session()

        def candles(self, *args):
            return bars([99, 101])

        def funding(self, *args):
            return []

        def orderbook(self, symbol):
            return self.get(URL, {"category": "linear", "symbol": symbol})

    source = Source()
    source.shared = seeded(tmp_path / "public.sqlite")
    blocker = sqlite3.connect(source.shared.path, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    saw_lock = False
    deadline = time.monotonic() + 6

    def should_stop():
        nonlocal saw_lock
        state = store.read()
        if not saw_lock and "кэш" in str(state["diagnostics"]):
            saw_lock = True
            blocker.rollback()
        return not state["positions"] or time.monotonic() > deadline

    try:
        run_loop(
            store.path.parent,
            cfg,
            store,
            source,
            Veles(),
            {},
            should_stop,
            lambda *a: 50,
            None,
        )
        assert saw_lock
        assert not store.read()["positions"]
        assert any(e.get("reason") == "TAKE_PROFIT" for e in store.history())
        store.audit()
    finally:
        blocker.close()
        source.close()


@pytest.mark.parametrize("hub_producer", [False, True])
@pytest.mark.parametrize("lease_mode", ["superseded", "expired"])
def test_superseded_http_lease_cannot_return_or_ack_success(
    tmp_path, monkeypatch, hub_producer, lease_mode
):
    """Direct workers and the independent Hub share the same commit fence."""
    monkeypatch.delenv("BCC_MARKET_HUB_MODE", raising=False)
    from bcc.market_hub import MarketDataHub

    path = tmp_path / "public.sqlite"
    source = PublicData()
    source.shared = PublicCache(path)
    observer = PublicCache(path)
    params = {"symbol": "ETHUSDT"}
    hub = MarketDataHub(path, source=source, max_workers=1) if hub_producer else None
    subscription = hub.registry.subscribe(URL, params) if hub is not None else None

    class Response:
        status_code = 200
        headers = {}

        def raise_for_status(self):
            pass

        def json(self):
            return {"uncommitted": True}

    class Session:
        def get(self, url, params, timeout, *, allow_redirects):
            assert allow_redirects is False
            with observer.db() as db:
                db.execute("BEGIN IMMEDIATE")
                lease = db.execute("SELECT k,owner FROM lease LIMIT 1").fetchone()
                assert lease is not None
                if lease_mode == "superseded":
                    db.execute("UPDATE lease SET owner=?,until=? WHERE k=?",
                               ("replacement-worker", time.time() + 20, lease[0]))
                else:
                    # No successor is needed to revoke an expired lease.
                    db.execute("UPDATE lease SET until=0 WHERE k=?", (lease[0],))
                db.commit()
            return Response()

        def close(self):
            pass

    source._thread.session = Session()
    try:
        if hub is not None:
            assert hub.run_once() == [(subscription, False)]
            with hub.registry._connect() as db:
                row = db.execute(
                    "SELECT last_error FROM hub_subscriptions WHERE k=?", (subscription,)
                ).fetchone()
            assert row and "не подтверждён транзакцией" in row[0]
        else:
            with pytest.raises(DataUnavailable, match="не подтверждён транзакцией"):
                source.get(URL, params)
        with observer.db() as db:
            assert db.execute("SELECT COUNT(*) FROM cache").fetchone()[0] == 0
            lease = db.execute("SELECT owner,until FROM lease").fetchone()
            if lease_mode == "superseded":
                assert lease[0] == "replacement-worker"
            else:
                assert lease[1] == 0
    finally:
        if hub is not None:
            hub.close()
        else:
            source.close()
        observer.close()


def test_cache_finish_reports_only_authorized_published_data(tmp_path):
    cache = PublicCache(tmp_path / "public.sqlite")
    try:
        key, owner, _ = cache.begin(URL, {"symbol": "SOLUSDT"}, HOST)
        assert cache.finish(key, "obsolete-owner", HOST, {"bad": True}, ttl=30) is False
        assert cache.finish(key, owner, HOST, {"good": True}, ttl=30) is True
        assert cache.read_fresh(URL, {"symbol": "SOLUSDT"}, HOST) == {"good": True}
    finally:
        cache.close()


def test_expired_singleflight_owner_cannot_extend_market_snapshot_ttl(tmp_path):
    """Expiry itself revokes publication, even without a successor owner."""
    cache = PublicCache(tmp_path / "public.sqlite")
    params = {"symbol": "BTCUSDT"}
    try:
        key, old, _ = cache.begin(URL, params, HOST)
        assert old
        with cache.db() as db:
            db.execute("UPDATE lease SET until=0 WHERE k=? AND owner=?", (key, old))
        assert cache.finish(key, old, HOST, {"stale": True},
                            ttl=60, cooldown=30) is False
        assert cache.read_fresh(URL, params, HOST) is None
        with cache.db() as db:
            assert db.execute("SELECT 1 FROM cooldown WHERE host=?", (HOST,)).fetchone() is None
        new_key, new, cached = cache.begin(URL, params, HOST)
        assert new_key == key and new != old and cached is None
        assert cache.finish(new_key, new, HOST, {"fresh": True}, ttl=30)
        assert cache.read_fresh(URL, params, HOST) == {"fresh": True}
    finally:
        cache.close()
