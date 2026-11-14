"""Cross-process public-read cache, single flight and exchange cooldowns."""

import hashlib
import json
import math
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path


class CacheBusy(RuntimeError):
    """SQLite contention, shared rate budget, or a competing request lease."""


class CacheContention(CacheBusy):
    """A retryable local/SQLite lock, never a provider quota or denial."""


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

    @staticmethod
    def _schema_current(db):
        """Read-only fast path for the fully migrated shared WAL schema.

        Fifteen Windows workers can attach at once. They must not each take
        an exclusive migration lock just to run CREATE IF NOT EXISTS against
        a database whose schema is already correct.
        """
        expected = {
            "cache": {"k", "value", "until", "host"},
            "lease": {"k", "owner", "until"},
            "cooldown": {"host", "until"},
            "provider_denial": {
                "host", "status", "until_mono", "recorded_mono",
                "until_utc", "boot_epoch",
            },
            "request_budget": {"host", "slot", "count"},
        }
        tables = {
            row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not set(expected) <= tables:
            return False
        for name, required in expected.items():
            rows = list(db.execute("PRAGMA table_info(" + name + ")"))
            columns = {row[1] for row in rows}
            if not required <= columns:
                return False
            pk_name = "k" if name in ("cache", "lease") else "host"
            if name == "request_budget":
                pk_name = "host"
            if not any(row[1] == pk_name and row[5] == 1 for row in rows):
                return False
        for index_name, table_name, columns in (
            ("cache_until", "cache", ("until",)),
            ("cache_host", "cache", ("host",)),
            ("lease_until", "lease", ("until",)),
        ):
            index = db.execute(
                "SELECT tbl_name FROM sqlite_master WHERE type='index' AND name=?",
                (index_name,),
            ).fetchone()
            if not index or index[0] != table_name:
                return False
            actual = tuple(
                row[2] for row in db.execute(
                    "PRAGMA index_info(" + index_name + ")"
                )
            )
            if actual != columns:
                return False
        return True

    def _ensure_schema(self):
        with self._setup_lock:
            if self._ready:
                return
            with self.db() as db:
                # Avoid a cross-process writer lock for an already-migrated
                # database. Schema discovery and index inspection are reads.
                mode = db.execute("PRAGMA journal_mode").fetchone()[0]
                if mode.lower() == "wal" and self._schema_current(db):
                    self._ready = True
                    return
                # A WAL-mode *change* requires an exclusive SQLite lock. The
                # HubRegistry may already have initialized this database and
                # workers may be reading it: never issue a redundant change.
                if mode.lower() != "wal":
                    mode = db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                    if mode.lower() != "wal":
                        raise CacheBusy("Не удалось включить совместный доступ к кэшу")
                # BEGIN IMMEDIATE is essential for concurrent Windows worker
                # startups. executescript() implicitly COMMITs an open SQLite
                # transaction, leaving CREATE/ALTER and column discovery
                # unprotected against another process running the migration.
                # Keep WAL setup outside the write transaction, then apply the
                # entire schema upgrade atomically with individual statements.
                db.execute("BEGIN IMMEDIATE")
                for statement in (
                    "CREATE TABLE IF NOT EXISTS cache("
                    "k TEXT PRIMARY KEY,value TEXT NOT NULL,until REAL NOT NULL,"
                    "host TEXT NOT NULL DEFAULT '')",
                    "CREATE TABLE IF NOT EXISTS lease("
                    "k TEXT PRIMARY KEY,owner TEXT NOT NULL,until REAL NOT NULL)",
                    "CREATE TABLE IF NOT EXISTS cooldown("
                    "host TEXT PRIMARY KEY,until REAL NOT NULL)",
                    "CREATE TABLE IF NOT EXISTS provider_denial("
                    "host TEXT PRIMARY KEY,status INTEGER NOT NULL,until_mono REAL NOT NULL,"
                    "recorded_mono REAL NOT NULL DEFAULT 0,until_utc REAL NOT NULL DEFAULT 0,"
                    "boot_epoch REAL NOT NULL DEFAULT 0)",
                    "CREATE TABLE IF NOT EXISTS request_budget("
                    "host TEXT PRIMARY KEY,slot INTEGER NOT NULL,count INTEGER NOT NULL)",
                    "CREATE INDEX IF NOT EXISTS cache_until ON cache(until)",
                    "CREATE INDEX IF NOT EXISTS lease_until ON lease(until)",
                ):
                    db.execute(statement)
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
                if "boot_epoch" not in columns:
                    db.execute("ALTER TABLE provider_denial ADD COLUMN "
                               "boot_epoch REAL NOT NULL DEFAULT 0")
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
                db.commit()
            self._ready = True

    @contextmanager
    def db(self):
        # One short SQL operation at a time within this process. Keeping the
        # connection open avoids the last-connection WAL checkpoint on every GET.
        if not self._guard.acquire(timeout=0.2):
            self.stats["local_busy"] += 1
            raise CacheContention("Общий кэш занят другим запросом этого процесса")
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
            # Only lock contention may be retried. Corrupt schemas, disk-full
            # errors and denied providers are not transient admission misses.
            code = getattr(exc, "sqlite_errorcode", None)
            lock_codes = (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
            is_lock = (code is not None and (code & 0xff) in lock_codes)
            error_type = CacheContention if is_lock else CacheBusy
            raise error_type(
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
    @lru_cache(maxsize=1)
    def _boot_epoch():
        """Process-stable OS boot witness; never rederive on a wall-clock jump.

        On Windows psutil.boot_time() can reflect a changed wall clock while
        this process and its monotonic clock have NOT rebooted. Capturing it
        once per process preserves same-boot HTTP 403/451 fences. A genuine
        reboot starts a fresh process and obtains a fresh witness.
        """
        try:
            import psutil
            value = float(psutil.boot_time())
            return value if math.isfinite(value) and value > 0 else 0.0
        except (ImportError, OSError, ValueError, AttributeError):
            return 0.0

    @staticmethod
    def _denied(db, host):
        row = db.execute(
            "SELECT status,until_mono,recorded_mono,until_utc,boot_epoch "
            "FROM provider_denial WHERE host=?", (host,)
        ).fetchone()
        if row is None:
            return None
        now_mono = time.monotonic()
        boot = PublicCache._boot_epoch()
        # A later reboot may already have MORE uptime than the previous
        # recorded_mono, so the old monotonic-reset heuristic is insufficient.
        rebooted = bool(row[4] and boot and abs(row[4] - boot) > 60)
        # Rows migrated from the legacy schema have no boot witness.
        if rebooted or (row[2] and now_mono < row[2]):
            return int(row[0]) if time.time() < row[3] else None
        return int(row[0]) if now_mono < row[1] else None

    def denied_status(self, host):
        """Read the fleet-wide provider fence despite transient SQLite contention.

        On clean Windows startup many independent workers simultaneously
        initialize the same WAL database. A 200 ms lock timeout is appropriate
        for one SQL operation, but cannot by itself prove that an HTTP 403/451
        policy fence is absent. Retry only busy/locked failures within a
        bounded admission window. Never return None on an unreadable fence,
        and never send a direct HTTP request to bypass it.
        """
        deadline = time.monotonic() + 5.0
        while True:
            try:
                self._ensure_schema()
                with self.db() as db:
                    return self._denied(db, host)
            except CacheBusy:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

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

    def publish_websocket(self, url, params, host, value, *, ttl, leader_owner, leader_token):
        """Atomic event-driven publication fenced by Hub leader and subscription.

        This does not create a fake HTTP request, consume its budget, or
        accept events from a producer that lost its cross-process leader lease.
        """
        from .market import PublicData
        PublicData.validated_public_request(url, params)
        binance = (host == "api.binance.com"
                   and url == "https://api.binance.com/api/v3/ticker/price"
                   and type(params) is dict and set(params) == {"symbol"}
                   and type(params.get("symbol")) is str)
        poly = (host == "clob.polymarket.com"
                and url == "https://clob.polymarket.com/book"
                and type(params) is dict and set(params) == {"token_id"}
                and type(params.get("token_id")) is str
                and params["token_id"].isdigit())
        bybit = (host == "api.bybit.com"
                and type(params) is dict and params.get("category") == "linear"
                and type(params.get("symbol")) is str
                and url in (
                    "https://api.bybit.com/v5/market/tickers",
                    "https://api.bybit.com/v5/market/orderbook",
                    "https://api.bybit.com/v5/market/kline",
                )
                and (url.endswith("/orderbook") is False or
                    (set(params) == {"category", "symbol", "limit"} and
                     params.get("limit") == 50))
                and (url.endswith("/kline") is False or
                    (set(params) == {"category", "symbol", "interval", "limit"} and
                     params.get("interval") == "1" and params.get("limit") == 10))
                and (url.endswith("/tickers") is False or
                    set(params) == {"category", "symbol"}))
        if ((not poly and not bybit and not binance)
                or type(ttl) not in (float, int) or not 0 < ttl <= 1
                or type(leader_owner) is not str or not leader_owner
                or type(leader_token) is not str or not leader_token):
            raise ValueError("Invalid WS publisher admission")
        marker = ("_bcc_ws_binance" if binance else
                  "_bcc_ws_polybook" if poly else
                  "_bcc_ws_book" if url.endswith("/orderbook") else
                  "_bcc_ws_kline" if url.endswith("/kline") else
                  "_bcc_ws_quote")
        if (type(value) is not dict or
                (value.get(marker) is not True and not (
                    bybit and url.endswith("/tickers") and
                    value.get("_bcc_ws_mark") is True))):
            raise ValueError("Unverified WS payload provenance")
        if bybit and url.endswith("/tickers") and value.get("_bcc_ws_mark") is True:
            # A mark-only record is not a quote. Validate its independent
            # price and do not infer a mark from executable bid/ask.
            rows = value.get("result", {}).get("list", [])
            if (type(rows) is not list or len(rows) != 1
                    or rows[0].get("symbol") != params["symbol"]):
                raise ValueError("Invalid WS mark-price symbol")
            try:
                mark = float(rows[0]["markPrice"])
            except (TypeError, ValueError, KeyError, OverflowError) as exc:
                raise ValueError("Invalid WS mark-price") from exc
            if not math.isfinite(mark) or mark <= 0:
                raise ValueError("Invalid WS mark-price")
            exchange_ms = value.get("_bcc_ws_mark_at")
            if type(exchange_ms) is not int or exchange_ms <= 0:
                raise ValueError("Missing independently timestamped WS mark-price")
        key = hashlib.sha256(
            json.dumps([url, params], sort_keys=True).encode()
        ).hexdigest()
        encoded = json.dumps(value, allow_nan=False)
        self._ensure_schema()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            boot = self._boot_epoch()
            admitted = db.execute(
                "SELECT 1 FROM hub_leader WHERE singleton=1 "
                "AND owner=? AND lease_token=? AND expires>? "
                "AND (boot_epoch=0 OR ?=0 OR abs(boot_epoch-?)<=60)",
                (leader_owner, leader_token, now, boot, boot),
            ).fetchone()
            subscribed = db.execute(
                "SELECT 1 FROM hub_subscriptions WHERE k=? "
                "AND active_until>? AND url=? AND params=?",
                (key, now, url, json.dumps(params, sort_keys=True,
                     separators=(",", ":"))),
            ).fetchone()
            if not admitted or not subscribed or self._denied(db, host) is not None:
                db.rollback()
                return False
            db.execute(
                "INSERT OR REPLACE INTO cache(k,value,until,host) "
                "VALUES(?,?,?,?)",
                (key, encoded, now + ttl, host),
            )
            db.commit()
        return True

    def begin(self, url, params, host, *, priority=False, websocket=False):
        if type(priority) is not bool or type(websocket) is not bool:
            raise ValueError('Invalid public request reservation')
        if websocket:
            # Only the known public Bybit linear ticker route may publish
            # through this budget-free WS lane. Never let arbitrary HTTP
            # endpoints opt out of their provider request quota.
            from .market import PublicData
            if type(params) is not dict:
                raise ValueError('WebSocket publication route not allowed')
            ticker = (url == "https://api.bybit.com/v5/market/tickers"
                      and set(params) == {"category", "symbol"})
            book = (url == "https://api.bybit.com/v5/market/orderbook"
                    and set(params) == {"category", "symbol", "limit"}
                    and type(params.get("limit")) is int and params["limit"] == 50)
            kline = (url == "https://api.bybit.com/v5/market/kline"
                     and set(params) == {"category", "symbol", "interval", "limit"}
                     and params.get("interval") == "1"
                     and type(params.get("limit")) is int and params["limit"] == 10)
            if (host != "api.bybit.com" or type(params) is not dict
                    or params.get("category") != "linear"
                    or not (ticker or book or kline)):
                raise ValueError('WebSocket publication route not allowed')
            PublicData.validated_public_request(url, params)
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
            if not websocket:
                slot = int(now)
                budget = db.execute(
                    "SELECT slot,count FROM request_budget WHERE host=?", (host,)
                ).fetchone()
                maximum = 10 if host == "api.binance.com" else 60
                count = budget[1] if budget and budget[0] == slot else 0
                # Reserve one fifth of HTTP Bybit request capacity for open
                # PAPER positions; real WS publications consume none of it.
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

    def _store_denial(self, db, host, cooldown, denial_status):
        """Atomically fence a provider and invalidate only its cached data.

        Called inside a SQLite BEGIN IMMEDIATE transaction, independently of
        whether an HTTP request still owns its short-lived cache lease.
        """
        if (type(host) is not str or not host
                or type(denial_status) is not int or denial_status not in (403, 451)
                or type(cooldown) not in (int, float)
                or not math.isfinite(cooldown) or not 0 < cooldown <= 3600):
            raise ValueError("Invalid public-provider denial")
        db.execute("DELETE FROM cache WHERE host=?", (host,))
        # Reconcile under the same SQLite write transaction.
        # Monotonic deadlines cannot be compared across boots.
        if self._denied(db, host) is None:
            db.execute("DELETE FROM provider_denial WHERE host=?", (host,))
        previous = db.execute(
            "SELECT status,until_mono,recorded_mono,until_utc,boot_epoch "
            "FROM provider_denial WHERE host=?", (host,)
        ).fetchone()
        now_mono = time.monotonic()
        now_utc = time.time()
        boot = self._boot_epoch()
        deadline_mono = now_mono + cooldown
        deadline_utc = now_utc + cooldown
        status = denial_status
        if previous is not None:
            if previous[0] == 451:
                status = 451
            deadline_utc = max(deadline_utc, previous[3])
            same_boot = (
                bool(boot and previous[4] and abs(boot - previous[4]) <= 60)
                or (not boot and not previous[4]
                    and now_mono >= previous[2])
            )
            if same_boot:
                deadline_mono = max(deadline_mono, previous[1])
            else:
                deadline_mono = now_mono + max(
                    cooldown, max(0, previous[3] - now_utc)
                )
        db.execute(
            "INSERT OR REPLACE INTO provider_denial("
            "host,status,until_mono,recorded_mono,until_utc,boot_epoch"
            ") VALUES(?,?,?,?,?,?)",
            (host, status, deadline_mono, now_mono, deadline_utc, boot),
        )

    def record_http_denial(self, key, owner, host, *, cooldown, denial_status):
        """Publish a real HTTP 403/451 even if its request lease has expired.

        The caller must have observed the upstream response. Request leases
        fence successful cache writes, not authoritative host-wide refusals.
        Releasing the old request must never delete a successor's lease.
        """
        if (type(key) is not str or len(key) != 64
                or any(ch not in "0123456789abcdef" for ch in key)
                or type(owner) is not str or not owner):
            raise ValueError("Invalid observed HTTP request identity")
        self._ensure_schema()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._store_denial(db, host, cooldown, denial_status)
            db.execute(
                "INSERT INTO cooldown VALUES(?,?) "
                "ON CONFLICT(host) DO UPDATE SET until=max(until,excluded.until)",
                (host, time.time() + cooldown),
            )
            db.execute("DELETE FROM lease WHERE k=? AND owner=?", (key, owner))
            db.commit()

    def finish(self, key, owner, host, value=None, ttl=0, cooldown=0,
               denial_status=None):
        """Return True only when this lease published its value to shared cache.

        A replaced lease or provider denial may discard a 200 HTTP response;
        callers must never treat that uncommitted payload as PAPER market data.
        """
        if owner is None:
            return False
        # Serialization may be expensive for a long candle history. It must not
        # run while blocking every other bot's short write transaction.
        encoded = (
            json.dumps(value, allow_nan=False)
            if value is not None and ttl > 0
            else None
        )
        self._ensure_schema()
        published = False
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT owner,until FROM lease WHERE k=?", (key,)).fetchone()
            # A matching owner is insufficient after its 20-second HTTP lease
            # expires. A stalled response must not acquire a fresh cache TTL
            # just because no replacement worker has claimed the key yet.
            if row and row[0] == owner and row[1] > time.time():
                # A second in-flight GET may complete after another request
                # from the same host was rejected. Never publish that older
                # response into the shared cache while the policy fence holds.
                if encoded is not None and self._denied(db, host) is None:
                    db.execute(
                        "INSERT OR REPLACE INTO cache(k,value,until,host) "
                        "VALUES(?,?,?,?)",
                        (key, encoded, time.time() + ttl, host),
                    )
                    published = True
                if cooldown:
                    db.execute(
                        "INSERT INTO cooldown VALUES(?,?) ON CONFLICT(host) DO UPDATE SET until=max(until,excluded.until)",
                        (host, time.time() + cooldown),
                    )
                if denial_status is not None:
                    self._store_denial(db, host, cooldown, denial_status)
                db.execute("DELETE FROM lease WHERE k=? AND owner=?", (key, owner))
                if time.monotonic() >= self._next_cleanup:
                    db.execute("DELETE FROM cache WHERE until<?", (time.time() - 120,))
                    db.execute("DELETE FROM lease WHERE until<?", (time.time(),))
                    self._next_cleanup = time.monotonic() + 30
                db.commit()
        return published
