"""Atomic schema migration across independent PAPER workers.

No network, trading credentials, accounts or real orders.  SQLite's autocommit
DDL and executescript() otherwise permit competing workers to inspect the same
legacy schema and race on ALTER TABLE, leaving partial migrations behind.
"""
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
import sqlite3
import time

from bcc.data_cache import CacheBusy, PublicCache
from bcc.market_hub import HubRegistry


URL = "https://api.bybit.com/v5/market/tickers"
PARAMS = {"category": "linear", "symbol": "BTCUSDT"}


class AssertTransactionalSchema:
    """Assert every schema read/write runs under the writer lock."""

    def __init__(self, connection):
        self.connection = connection

    def execute(self, statement, *args):
        sql = statement.strip().upper()
        if (sql.startswith(("CREATE ", "ALTER ", "UPDATE PROVIDER_DENIAL",
                            "UPDATE HUB_SUBSCRIPTIONS", "PRAGMA TABLE_INFO"))):
            assert self.connection.in_transaction, sql
        return self.connection.execute(statement, *args)

    def __getattr__(self, name):
        if name == "executescript":
            raise AssertionError("executescript commits the migration transaction")
        return getattr(self.connection, name)


def _legacy_database(path):
    key, encoded = HubRegistry.identity(URL, PARAMS)
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE provider_denial("
            "host TEXT PRIMARY KEY,status INTEGER NOT NULL,until_mono REAL NOT NULL)"
        )
        db.execute(
            "INSERT INTO provider_denial VALUES(?,?,?)",
            ("api.bybit.com", 451, time.monotonic() + 600),
        )
        db.execute(
            "CREATE TABLE hub_subscriptions("
            "k TEXT PRIMARY KEY,url TEXT NOT NULL,params TEXT NOT NULL,"
            "active_until REAL NOT NULL,next_due REAL NOT NULL DEFAULT 0,"
            "last_error TEXT NOT NULL DEFAULT '')"
        )
        db.execute(
            "INSERT INTO hub_subscriptions(k,url,params,active_until) VALUES(?,?,?,?)",
            (key, URL, encoded, time.time() + 600),
        )
        db.execute(
            "CREATE TABLE hub_leader("
            "singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
            "owner TEXT NOT NULL,expires REAL NOT NULL)"
        )
    return key


def test_both_schema_migrations_hold_one_sqlite_writer_transaction(tmp_path, monkeypatch):
    path = tmp_path / "shared.sqlite"
    key = _legacy_database(path)

    original_hub = HubRegistry._connect

    @contextmanager
    def traced_hub(self):
        with original_hub(self) as db:
            yield AssertTransactionalSchema(db)

    with monkeypatch.context() as patch:
        patch.setattr(HubRegistry, "_connect", traced_hub)
        hub = HubRegistry(path)

    cache = PublicCache(path)
    original_cache = cache.db

    @contextmanager
    def traced_cache():
        with original_cache() as db:
            yield AssertTransactionalSchema(db)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(cache, "db", traced_cache)
            assert cache.denied_status("api.bybit.com") == 451
        assert hub.due(include_generation=True)[0][-1]
        with sqlite3.connect(path) as db:
            assert db.execute(
                "SELECT subscription_token FROM hub_subscriptions WHERE k=?", (key,)
            ).fetchone()[0]
            assert {"recorded_mono", "until_utc", "boot_epoch"} <= {
                row[1] for row in db.execute("PRAGMA table_info(provider_denial)")
            }
            assert {"lease_token", "boot_epoch"} <= {
                row[1] for row in db.execute("PRAGMA table_info(hub_leader)")
            }
    finally:
        cache.close()


def _worker_bootstrap(path):
    """Top-level function works with Windows spawn as well as POSIX fork."""
    for attempt in range(12):
        cache = None
        try:
            hub = HubRegistry(path)
            cache = PublicCache(path)
            assert cache.denied_status("api.bybit.com") == 451
            assert hub.due(include_generation=True)[0][-1]
            return True
        except (sqlite3.OperationalError, CacheBusy) as exc:
            if not any(word in str(exc).lower() for word in ("busy", "locked", "занят")):
                raise
            time.sleep(.05 * (attempt + 1))
        finally:
            if cache is not None:
                cache.close()
    raise AssertionError("Shared schema migration did not converge after contention")


def test_legacy_schema_converges_across_independent_processes(tmp_path):
    path = tmp_path / "shared.sqlite"
    key = _legacy_database(path)
    # Each process has its own Python locks and SQLite connection; a
    # single-process thread test cannot reproduce this startup race.
    with ProcessPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(_worker_bootstrap, [str(path)] * 8))
    assert all(results)
    with sqlite3.connect(path) as db:
        row = db.execute(
            "SELECT subscription_token FROM hub_subscriptions WHERE k=?", (key,)
        ).fetchone()
        assert row and len(row[0]) == 32
        assert db.execute(
            "SELECT status FROM provider_denial WHERE host='api.bybit.com'"
        ).fetchone() == (451,)
