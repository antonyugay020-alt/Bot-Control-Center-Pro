"""Cross-process public-read cache, single flight and exchange cooldowns."""

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class CacheBusy(RuntimeError):
    """SQLite contention, shared rate budget, or a competing request lease."""


class ProviderDenied(RuntimeError):
    """A structured policy fence, distinct from retryable cache contention."""

    def __init__(self, host, http_status):
        self.host = host
        self.http_status = int(http_status)
        super().__init__(f"Источник отклонил публичный доступ: {host} (HTTP {http_status})")


class PublicCache:
    _guards = {}
    _guards_lock = threading.Lock()

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ready = False
        self._setup_lock = threading.Lock()
        self._next_cleanup = 0
        with self._guards_lock:
            self._guard = self._guards.setdefault(
                str(self.path.resolve()), threading.RLock()
            )
        self._connection = None
        self.stats = {
            "connections": 0,
            "operations": 0,
            "local_busy": 0,
            "sqlite_busy": 0,
        }

    def _ensure_schema(self):
        with self._setup_lock:
            if self._ready:
                return
            with self.db() as db:
                # A WAL-mode *change* requires an exclusive SQLite lock. The
                # HubRegistry may already have initialized this database and
                # workers may be reading it: never issue a redundant change.
                mode = db.execute("PRAGMA journal_mode").fetchone()[0]
                if mode.lower() != "wal":
                    mode = db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                    if mode.lower() != "wal":
                        raise CacheBusy("Не удалось включить совместный доступ к кэшу")
                db.executescript(
                    "CREATE TABLE IF NOT EXISTS cache("
                    "k TEXT PRIMARY KEY,value TEXT NOT NULL,until REAL NOT NULL,"
                    "host TEXT NOT NULL DEFAULT '');"
                    "CREATE TABLE IF NOT EXISTS lease(k TEXT PRIMARY KEY,owner TEXT NOT NULL,until REAL NOT NULL);"
                    "CREATE TABLE IF NOT EXISTS cooldown(host TEXT PRIMARY KEY,until REAL NOT NULL);"
                    "CREATE TABLE IF NOT EXISTS provider_denial("
                    "host TEXT PRIMARY KEY,status INTEGER NOT NULL,until_mono REAL NOT NULL,"
                    "recorded_mono REAL NOT NULL DEFAULT 0,until_utc REAL NOT NULL DEFAULT 0);"
                    "CREATE TABLE IF NOT EXISTS request_budget(host TEXT PRIMARY KEY,slot INTEGER NOT NULL,count INTEGER NOT NULL);"
                    "CREATE INDEX IF NOT EXISTS cache_until ON cache(until);"
                    "CREATE INDEX IF NOT EXISTS lease_until ON lease(until);"
                )
                # The previous cache schema did not record provider ownership
                # for each response. Legacy rows have an empty host and must
                # never be admitted as verified host-specific market data.
                cache_columns = {
                    row[1] for row in db.execute("PRAGMA table_info(cache)")
                }
                if "host" not in cache_columns:
                    db.execute(
                        "ALTER TABLE cache ADD COLUMN host TEXT NOT NULL DEFAULT ''"
                    )
                db.execute("CREATE INDEX IF NOT EXISTS cache_host ON cache(host)")
                # An existing shared cache can have the original three-column
                # provider_denial table. Migrate in place, preserving active
                # refusals across upgrades and independently running workers.
                columns = {
                    row[1] for row in db.execute("PRAGMA table_info(provider_denial)")
                }
                if "recorded_mono" not in columns:
                    db.execute(
                        "ALTER TABLE provider_denial ADD COLUMN "
                        "recorded_mono REAL NOT NULL DEFAULT 0"
                    )
                if "until_utc" not in columns:
                    db.execute(
                        "ALTER TABLE provider_denial ADD COLUMN "
                        "until_utc REAL NOT NULL DEFAULT 0"
                    )
                # Old rows only had monotonic deadlines. Conservatively carry
                # their remaining refusal into the new UTC fallback, bounded
                # by the maximum supported 403/451 policy interval (3600s).
                # Without this, a restart immediately after upgrading could
                # discard a refusal or retain a previous boot's uptime forever.
                if "recorded_mono" not in columns or "until_utc" not in columns:
                    db.execute(
                        "UPDATE provider_denial SET recorded_mono=?, "
                        "until_utc=? + min(3600,max(0,until_mono-?)) "
                        "WHERE recorded_mono=0 AND until_utc=0",
                        (time.monotonic(), time.time(), time.monotonic()),
                    )
            self._ready = True

    @contextmanager
    def db(self):
        # One short SQL operation at a time within this process. Keeping the
        # connection open avoids the last-connection WAL checkpoint on every GET.
        if not self._guard.acquire(timeout=0.2):
            self.stats["local_busy"] += 1
            raise CacheBusy("Общий кэш занят другим запросом этого процесса")
        try:
            if self._connection is None:
                self._connection = sqlite3.connect(
                    self.path,
                    timeout=0.2,
                    isolation_level=None,
                    check_same_thread=False,
                )
                self.stats["connections"] += 1
                self._connection.execute("PRAGMA busy_timeout=200")
                self._connection.execute("PRAGMA synchronous=NORMAL")
            self.stats["operations"] += 1
            yield self._connection
        except sqlite3.Error as exc:
            self.stats["sqlite_busy"] += 1
            raise CacheBusy(
                "Общий кэш публичных данных недоступен: " + str(exc)
            ) from exc
        finally:
            try:
                if self._connection is not None and self._connection.in_transaction:
                    self._connection.rollback()
            finally:
                self._guard.release()

    def close(self):
        with self._guard:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
                self._ready = False

    @staticmethod
    def _denied(db, host):
        row = db.execute(
            "SELECT status,until_mono,recorded_mono,until_utc "
            "FROM provider_denial WHERE host=?", (host,)
        ).fetchone()
        if row is None:
            return None
        now_mono = time.monotonic()
        if row[2] and now_mono < row[2]:
            # Monotonic time resets after an OS restart. An old uptime-based
            # deadline cannot be compared with a new boot's uptime. For this
            # case only, use the persisted UTC deadline rather than retaining
            # a denial for the previous boot's entire uptime.
            return int(row[0]) if time.time() < row[3] else None
        return int(row[0]) if now_mono < row[1] else None

    def denied_status(self, host):
        """Share policy refusals across workers, with restart-aware expiry."""
        self._ensure_schema()
        with self.db() as db:
            return self._denied(db, host)

    @staticmethod
    def _cached(db, key, host, now):
        # A public response already cached inside its validated TTL does not
        # consume exchange request budget. A remote cooldown must prevent new
        # HTTP requests, not invalidate data that was fetched before it.
        status = PublicCache._denied(db, host)
        if status is not None:
            raise ProviderDenied(host, status)
        row = db.execute(
            "SELECT value,until FROM cache WHERE k=? AND host=?", (key, host)
        ).fetchone()
        if row and row[1] > now:
            return row[0]
        cooldown = db.execute(
            "SELECT until FROM cooldown WHERE host=?", (host,)
        ).fetchone()
        if cooldown and cooldown[0] > now:
            raise CacheBusy("Общий лимит запросов " + host)
        return None

    def read_fresh(self, url, params, host):
        """Read-only cache lookup; never reserves a lease or request budget."""
        key = hashlib.sha256(
            json.dumps([url, params], sort_keys=True).encode()
        ).hexdigest()
        self._ensure_schema()
        with self.db() as db:
            status = self._denied(db, host)
            if status is not None:
                raise ProviderDenied(host, status)
            row = db.execute(
                "SELECT value FROM cache WHERE k=? AND host=? AND until>?",
                (key, host, time.time()),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def begin(self, url, params, host, *, priority=False):
        if type(priority) is not bool:
            raise ValueError('Public GET priority must be boolean')
        key = hashlib.sha256(
            json.dumps([url, params], sort_keys=True).encode()
        ).hexdigest()
        self._ensure_schema()
        owner = uuid.uuid4().hex
        with self.db() as db:
            # Cache hits are read-only. Only a miss reserves the writer for the
            # atomic lease and host budget, then releases it before HTTP/JSON.
            value = self._cached(db, key, host, time.time())
            if value is not None:
                return key, None, json.loads(value)
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            value = self._cached(db, key, host, now)
            if value is not None:
                db.commit()
                return key, None, json.loads(value)
            lease = db.execute("SELECT until FROM lease WHERE k=?", (key,)).fetchone()
            if lease and lease[0] > now:
                raise CacheBusy("Данные уже запрашиваются другим ботом")
            slot = int(now)
            budget = db.execute(
                "SELECT slot,count FROM request_budget WHERE host=?", (host,)
            ).fetchone()
            maximum = 10 if host == "api.binance.com" else 60
            count = budget[1] if budget and budget[0] == slot else 0
            # Keep one fifth of Bybit's existing shared request budget for
            # executable books of OPEN PAPER positions. Background scanners
            # cannot consume the reserved capacity, while priority requests
            # still obey the same hard 60-per-second total budget.
            reserved = 12 if host == "api.bybit.com" else 0
            allowed = maximum if priority else maximum - reserved
            if count >= allowed:
                raise CacheBusy("Общий бюджет публичных запросов исчерпан; ожидание")
            db.execute(
                "INSERT OR REPLACE INTO request_budget VALUES(?,?,?)",
                (host, slot, count + 1),
            )
            db.execute(
                "INSERT OR REPLACE INTO lease VALUES(?,?,?)", (key, owner, now + 20)
            )
            db.commit()
        return key, owner, None

    def finish(self, key, owner, host, value=None, ttl=0, cooldown=0,
               denial_status=None):
        if owner is None:
            return
        # Serialization may be expensive for a long candle history. It must not
        # run while blocking every other bot's short write transaction.
        encoded = (
            json.dumps(value, allow_nan=False)
            if value is not None and ttl > 0
            else None
        )
        self._ensure_schema()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT owner FROM lease WHERE k=?", (key,)).fetchone()
            if row and row[0] == owner:
                # A second in-flight GET may complete after another request
                # from the same host was rejected. Never publish that older
                # response into the shared cache while the policy fence holds.
                if encoded is not None and self._denied(db, host) is None:
                    db.execute(
                        "INSERT OR REPLACE INTO cache(k,value,until,host) "
                        "VALUES(?,?,?,?)",
                        (key, encoded, time.time() + ttl, host),
                    )
                if cooldown:
                    db.execute(
                        "INSERT INTO cooldown VALUES(?,?) ON CONFLICT(host) DO UPDATE SET until=max(until,excluded.until)",
                        (host, time.time() + cooldown),
                    )
                if denial_status is not None:
                    if denial_status not in (403, 451) or cooldown <= 0:
                        raise ValueError("Invalid public-provider denial")
                    # Atomic with lease release: another worker cannot read a
                    # cached response or reserve a request after the refusal.
                    # Invalidate only this host's cached market responses.
                    # They must not become available again after the denial
                    # expires without a fresh authorized upstream GET.
                    db.execute("DELETE FROM cache WHERE host=?", (host,))
                    # Replace an expired policy row before extending a fresh
                    # denial. This also prevents pre-reboot uptime values from
                    # leaking into a newly established refusal.
                    if self._denied(db, host) is None:
                        db.execute("DELETE FROM provider_denial WHERE host=?", (host,))
                    now_mono = time.monotonic()
                    now_utc = time.time()
                    db.execute(
                        "INSERT INTO provider_denial("
                        "host,status,until_mono,recorded_mono,until_utc"
                        ") VALUES(?,?,?,?,?) "
                        "ON CONFLICT(host) DO UPDATE SET "
                        "status=CASE WHEN provider_denial.status=451 OR "
                        "excluded.status=451 THEN 451 ELSE 403 END,"
                        "recorded_mono=CASE WHEN excluded.until_mono>until_mono "
                        "THEN excluded.recorded_mono ELSE recorded_mono END,"
                        "until_mono=max(until_mono,excluded.until_mono),"
                        "until_utc=max(until_utc,excluded.until_utc)",
                        (host, denial_status, now_mono + cooldown,
                         now_mono, now_utc + cooldown),
                    )
                db.execute("DELETE FROM lease WHERE k=? AND owner=?", (key, owner))
                if time.monotonic() >= self._next_cleanup:
                    db.execute("DELETE FROM cache WHERE until<?", (time.time() - 120,))
                    db.execute("DELETE FROM lease WHERE until<?", (time.time(),))
                    self._next_cleanup = time.monotonic() + 30
                db.commit()
