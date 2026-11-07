"""Atomic schema migration across independent PAPER workers.

No network, trading credentials, accounts or real orders.  SQLite's autocommit
DDL and executescript() otherwise permit competing workers to inspect the same
legacy schema and race on ALTER TABLE, leaving partial migrations behind.
"""
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
import sqlite3
import time

import pytest

from bcc.data_cache import CacheBusy, PublicCache
from bcc.market_hub import HubRegistry


URL = "https://api.bybit.com/v5/market/tickers"
PARAMS = {"category": "linear", "symbol": "BTCUSDT"}


class AssertTransactionalSchema:
    """DDL and legacy backfills require the writer lock; preflight reads do not."""

    def __init__(self, connection):
        self.connection = connection

    def execute(self, statement, *args):
        sql = statement.strip().upper()
        if (sql.startswith(("CREATE ", "ALTER ", "UPDATE PROVIDER_DENIAL",
                            "UPDATE HUB_SUBSCRIPTIONS", "DROP INDEX"))):
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


def test_migrated_hub_registry_opens_while_another_process_owns_writer(tmp_path):
    """Ready-schema admission is read-only, even under a live SQLite writer.

    This reproduces the startup contention of many independent PAPER workers
    while the Market Data Hub producer updates subscriptions in WAL mode.
    """
    path = tmp_path / "market.sqlite"
    first = HubRegistry(path)
    assert first.subscribe(URL, PARAMS)

    writer = sqlite3.connect(path, timeout=0.1, isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        # A second registry must not request BEGIN IMMEDIATE: it would block
        # behind this writer and fail its one-second connection timeout.
        other = HubRegistry(path)
        assert other.has_active_subscriptions()
    finally:
        writer.rollback()
        writer.close()


def test_hub_fast_path_falls_back_for_incomplete_index_or_token(tmp_path):
    """The read-only shortcut cannot skip interrupted legacy migrations."""
    path = tmp_path / "market.sqlite"
    registry = HubRegistry(path)
    key = registry.subscribe(URL, PARAMS)
    db = sqlite3.connect(path)
    try:
        db.execute("DROP INDEX hub_expiry")
        db.execute(
            "UPDATE hub_subscriptions SET subscription_token='' WHERE k=?", (key,)
        )
        db.commit()
    finally:
        db.close()

    repaired = HubRegistry(path)
    with repaired._connect() as connection:
        assert connection.execute(
            "SELECT subscription_token FROM hub_subscriptions WHERE k=?", (key,)
        ).fetchone()[0]
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='hub_expiry'"
        ).fetchone() == (1,)


def test_hub_rebuilds_misnamed_queue_indexes_atomically(tmp_path):
    """An existing index name is not evidence of its SQL query-plan columns."""
    path = tmp_path / "market.sqlite"
    first = HubRegistry(path)
    key = first.subscribe(URL, PARAMS)
    with sqlite3.connect(path) as db:
        db.execute("DROP INDEX hub_due")
        db.execute("DROP INDEX hub_expiry")
        db.execute("CREATE INDEX hub_due ON hub_subscriptions(active_until,next_due)")
        db.execute("CREATE INDEX hub_expiry ON hub_subscriptions(next_due)")
    # The fast path must reject both wrong definitions. Migration rebuilds
    # both indexes inside the same writer transaction, preserving subscriber.
    repaired = HubRegistry(path)
    with repaired._connect() as db:
        assert tuple(r[0] for r in db.execute(
            "SELECT name FROM pragma_index_info('hub_due') ORDER BY seqno"
        )) == ("next_due", "active_until")
        assert tuple(r[0] for r in db.execute(
            "SELECT name FROM pragma_index_info('hub_expiry') ORDER BY seqno"
        )) == ("active_until",)
        assert db.execute(
            "SELECT subscription_token FROM hub_subscriptions WHERE k=?", (key,)
        ).fetchone()[0]


def test_hub_rejects_incompatible_primary_key_without_partial_migration(tmp_path):
    """An additive migration cannot invent a missing durable identity key."""
    path = tmp_path / "market.sqlite"
    HubRegistry(path)
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE hub_subscriptions")
        db.execute(
            "CREATE TABLE hub_subscriptions("
            "k TEXT NOT NULL, url TEXT NOT NULL, params TEXT NOT NULL,"
            "active_until REAL NOT NULL, next_due REAL NOT NULL DEFAULT 0,"
            "last_error TEXT NOT NULL DEFAULT '',"
            "priority_until REAL NOT NULL DEFAULT 0,"
            "subscription_token TEXT NOT NULL DEFAULT '')"
        )
    with pytest.raises(sqlite3.DatabaseError, match="schema integrity check failed"):
        HubRegistry(path)
    with sqlite3.connect(path) as db:
        assert not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' "
            "AND name IN ('hub_due','hub_expiry')"
        ).fetchone()
        assert not any(row[5] for row in db.execute(
            "PRAGMA table_info(hub_subscriptions)"
        ))


def test_hub_rejects_foreign_index_name_without_touching_it(tmp_path):
    """Do not destroy another table's index to make a broken schema boot."""
    path = tmp_path / "market.sqlite"
    HubRegistry(path)
    with sqlite3.connect(path) as db:
        db.execute("DROP INDEX hub_due")
        db.execute("CREATE TABLE other_component(id INTEGER)")
        db.execute("CREATE INDEX hub_due ON other_component(id)")
    with pytest.raises(sqlite3.DatabaseError, match="owned by another table"):
        HubRegistry(path)
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT tbl_name FROM sqlite_master WHERE type='index' AND name='hub_due'"
        ).fetchone() == ("other_component",)


@pytest.mark.parametrize('table', ['hub_subscriptions', 'hub_leader'])
def test_composite_identity_is_rejected_and_migration_rolls_back(tmp_path, table):
    """PK ordinal 1 alone cannot authorize ON CONFLICT(k/singleton)."""
    path = tmp_path / 'market.sqlite'
    HubRegistry(path)
    with sqlite3.connect(path) as db:
        db.execute('DROP TABLE ' + table)
        if table == 'hub_subscriptions':
            db.execute("CREATE TABLE hub_subscriptions("
                       "k TEXT, url TEXT NOT NULL, params TEXT NOT NULL,"
                       "active_until REAL NOT NULL, next_due REAL NOT NULL DEFAULT 0,"
                       "last_error TEXT NOT NULL DEFAULT '', PRIMARY KEY(k,url))")
        else:
            db.execute("CREATE TABLE hub_leader(singleton INTEGER, owner TEXT NOT NULL,"
                       "expires REAL NOT NULL, PRIMARY KEY(singleton,owner))")
        before = db.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name').fetchall()
    with pytest.raises(sqlite3.DatabaseError, match='schema integrity check failed'):
        HubRegistry(path)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name').fetchall() == before


@pytest.mark.parametrize('index,columns', [
    ('hub_due', 'next_due,active_until'), ('hub_expiry', 'active_until')])
@pytest.mark.parametrize('unique,partial', [(True, False), (False, True), (True, True)])
def test_queue_index_semantics_repaired_without_losing_live_generations(
        tmp_path, monkeypatch, index, columns, unique, partial):
    """Repair must preserve leaders/subscribers and admit equal deadlines."""
    path = tmp_path / 'market.sqlite'
    hub = HubRegistry(path)
    key = hub.subscribe(URL, PARAMS)
    assert hub.claim_leader('producer')
    lease = hub.leader_token('producer')
    with hub._connect() as db:
        generation, expiry = db.execute(
            'SELECT subscription_token,active_until FROM hub_subscriptions WHERE k=?', (key,)
        ).fetchone()
        db.execute('DROP INDEX ' + index)
        db.execute('CREATE ' + ('UNIQUE ' if unique else '') + 'INDEX ' + index +
                   ' ON hub_subscriptions(' + columns + ')' +
                   (' WHERE active_until>0' if partial else ''))
    repaired = HubRegistry(path)
    assert repaired.leader_token('producer') == lease
    with repaired._connect() as db:
        assert db.execute('SELECT subscription_token,active_until FROM hub_subscriptions WHERE k=?',
                          (key,)).fetchone() == (generation, expiry)
        shape = next(row for row in db.execute('PRAGMA index_list(hub_subscriptions)')
                     if row[1] == index)
        assert shape[2] == 0 and shape[4] == 0
    # Two markets may renew at the exact same time; neither is a duplicate.
    now = time.time()
    monkeypatch.setattr('bcc.market_hub.time.time', lambda: now)
    for symbol in ('ETHUSDT', 'SOLUSDT'):
        repaired.subscribe(URL, {**PARAMS, 'symbol': symbol})
    assert len(repaired.due()) == 3
