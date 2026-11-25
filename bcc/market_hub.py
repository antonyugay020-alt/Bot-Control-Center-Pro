"""Optional cross-process public-data hub transport (PAPER; no account writes).

Workers register validated public GET requests.  Only the hub producer performs
HTTP; results pass through PublicCache's existing TTL/budget/lease controls.
The mode is explicitly opt-in until native Windows lifecycle E2E is accepted.
"""
import json
import math
import os
import uuid
import sqlite3
import time
import threading
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .data_cache import PublicCache, CacheBusy


class HubRegistry:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    @staticmethod
    def _boot_epoch():
        # Use the same independent OS-boot witness as the provider-denial
        # fence. If unavailable, do not speculate that a lease is stale.
        return PublicCache._boot_epoch()

    @staticmethod
    def _same_boot(recorded, current):
        # A missing witness (legacy row or unavailable psutil) cannot prove a
        # reboot. Retain the ordinary bounded wall-clock lease in that case.
        return not (recorded and current and abs(recorded - current) > 60)

    @contextmanager
    def _connect(self):
        """Bounded ownership of each SQLite handle, including native Windows.

        sqlite3.Connection.__exit__ commits or rolls back but DOES NOT close.
        The Hub registry makes short-lived cross-process calls for every
        heartbeat, subscription and poll; leaking those connections can keep
        WAL and DB handles live after the Qt supervisor shuts down.
        """
        db = sqlite3.connect(self.path, timeout=1.0, isolation_level=None)
        try:
            db.execute('PRAGMA busy_timeout=1000')
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _index_matches(db, name, columns):
        """Verify index ownership AND ordered columns, not merely its name."""
        row = db.execute(
            "SELECT tbl_name FROM sqlite_master WHERE type='index' AND name=?",
            (name,),
        ).fetchone()
        # Same columns do not make a UNIQUE or partial index equivalent:
        # uniqueness rejects unrelated subscriptions sharing a deadline;
        # a predicate can omit the very expired rows maintenance must prune.
        shape = next((item for item in db.execute(
            "PRAGMA index_list(hub_subscriptions)"
        ) if item[1] == name), None)
        return bool(row and row[0] == 'hub_subscriptions' and shape
                    and not shape[2] and not shape[4] and
                    tuple(item[0] for item in db.execute(
                        'SELECT name FROM pragma_index_info(?) ORDER BY seqno',
                        (name,),
                    )) == tuple(columns))

    @staticmethod
    def _schema_current(db):
        """Read-only fast path for already migrated multi-worker databases.

        Thousands of independent workers can open the same Hub registry at
        startup. Taking BEGIN IMMEDIATE for every schema inspection serializes
        otherwise independent readers behind the producer's writer lock.
        A complete inventory (columns, indices, and token backfill) is the
        admission condition; legacy or partially migrated schemas still take
        the atomic writer-locked migration path below.
        """
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('hub_subscriptions','hub_leader')"
        )}
        if tables != {'hub_subscriptions', 'hub_leader'}:
            return False
        subscriptions = {row[1]: row for row in db.execute(
            'PRAGMA table_info(hub_subscriptions)'
        )}
        leaders = {row[1]: row for row in db.execute(
            'PRAGMA table_info(hub_leader)'
        )}
        if not {
            'k', 'url', 'params', 'active_until', 'next_due',
            'last_error', 'priority_until', 'subscription_token'
        } <= subscriptions.keys():
            return False
        if not {'singleton', 'owner', 'expires', 'lease_token', 'boot_epoch'} <= leaders.keys():
            return False
        # Columns without their primary-key contract can appear valid while
        # ON CONFLICT updates silently fail or admit duplicate leaders.
        if ({name: row[5] for name, row in subscriptions.items() if row[5]} != {'k': 1}
                or {name: row[5] for name, row in leaders.items() if row[5]} != {'singleton': 1}):
            return False
        for name in ('url', 'params', 'active_until', 'next_due',
                     'last_error', 'priority_until', 'subscription_token'):
            if subscriptions[name][3] != 1:
                return False
        for name in ('owner', 'expires', 'lease_token', 'boot_epoch'):
            if leaders[name][3] != 1:
                return False
        if not (HubRegistry._index_matches(db, 'hub_due', ('next_due', 'active_until'))
                and HubRegistry._index_matches(db, 'hub_expiry', ('active_until',))):
            return False
        return db.execute(
            "SELECT 1 FROM hub_subscriptions "
            "WHERE subscription_token='' LIMIT 1"
        ).fetchone() is None

    def _init(self):
        with self._connect() as db:
            # A journal-mode *change* needs exclusive access on Windows. The
            # supervisor also reads this schema while the producer starts:
            # do not try to reconfigure WAL on every readiness poll.
            mode = db.execute('PRAGMA journal_mode').fetchone()[0]
            if mode.lower() != 'wal':
                mode = db.execute('PRAGMA journal_mode=WAL').fetchone()[0]
                if mode.lower() != 'wal':
                    raise sqlite3.OperationalError('Market Hub requires SQLite WAL mode')
            # Existing migrated databases need no writer transaction just
            # to register another independent PAPER worker. The read-only
            # preflight can coexist with a producer's active SQLite writer.
            if self._schema_current(db):
                return
            # Missing columns, indices or legacy tokens require migration.
            # Recheck and apply DDL/backfill under ONE SQLite writer lock:
            # concurrent processes may all observe the same old schema.
            # WAL setup above must remain outside this transaction.
            db.execute('BEGIN IMMEDIATE')
            db.execute('''CREATE TABLE IF NOT EXISTS hub_subscriptions(
              k TEXT PRIMARY KEY, url TEXT NOT NULL, params TEXT NOT NULL,
              active_until REAL NOT NULL, next_due REAL NOT NULL DEFAULT 0,
              last_error TEXT NOT NULL DEFAULT '',
              priority_until REAL NOT NULL DEFAULT 0,
              subscription_token TEXT NOT NULL DEFAULT '')''')
            columns = {row[1] for row in db.execute('PRAGMA table_info(hub_subscriptions)')}
            if 'priority_until' not in columns:
                db.execute('ALTER TABLE hub_subscriptions ADD COLUMN priority_until REAL NOT NULL DEFAULT 0')
            if 'subscription_token' not in columns:
                db.execute("ALTER TABLE hub_subscriptions ADD COLUMN subscription_token TEXT NOT NULL DEFAULT ''")
            # An interrupted legacy backfill is completed atomically here.
            # Subsequent startups use the read-only fast path above.
            if db.execute("SELECT 1 FROM hub_subscriptions WHERE subscription_token='' LIMIT 1").fetchone():
                db.execute("UPDATE hub_subscriptions SET subscription_token=lower(hex(randomblob(16))) "
                           "WHERE subscription_token=''")
            # A named index is not proof of the queue/expiry access path.
            # Repair malformed same-table indexes under the migration writer
            # lock; never drop an index belonging to an unrelated table.
            for name, columns in (
                    ('hub_due', ('next_due', 'active_until')),
                    ('hub_expiry', ('active_until',))):
                existing = db.execute(
                    "SELECT tbl_name FROM sqlite_master WHERE type='index' AND name=?",
                    (name,),
                ).fetchone()
                if existing and existing[0] != 'hub_subscriptions':
                    raise sqlite3.DatabaseError(
                        'Market Hub schema integrity: index name owned by another table: ' + name
                    )
                if existing and not self._index_matches(db, name, columns):
                    db.execute('DROP INDEX "' + name + '"')
                db.execute('CREATE INDEX IF NOT EXISTS "' + name +
                           '" ON hub_subscriptions(' + ','.join(columns) + ')')
            db.execute("CREATE TABLE IF NOT EXISTS hub_leader(singleton INTEGER PRIMARY KEY CHECK(singleton=1), owner TEXT NOT NULL, expires REAL NOT NULL, lease_token TEXT NOT NULL DEFAULT '', boot_epoch REAL NOT NULL DEFAULT 0)")
            # Existing installations may have a live legacy leader row. The
            # next admission mints a new token; no old producer can prove it.
            leader_columns = {row[1] for row in db.execute('PRAGMA table_info(hub_leader)')}
            if 'lease_token' not in leader_columns:
                db.execute("ALTER TABLE hub_leader ADD COLUMN lease_token TEXT NOT NULL DEFAULT ''")
            if 'boot_epoch' not in leader_columns:
                db.execute("ALTER TABLE hub_leader ADD COLUMN boot_epoch REAL NOT NULL DEFAULT 0")
            # Recheck the complete schema while holding the same writer lock.
            # An incompatible primary key/NOT NULL contract cannot be safely
            # repaired by additive ALTER TABLE: fail closed, rollback the DDL,
            # and require an explicit migration rather than starting a Hub
            # that will fail at runtime under fleet load.
            if not self._schema_current(db):
                raise sqlite3.DatabaseError(
                    'Market Hub schema integrity check failed; refusing startup'
                )

    def claim_leader(self, owner, *, lifetime=15):
        if not owner or not 1 <= lifetime <= 120:
            raise ValueError('Invalid hub lease')
        candidate = uuid.uuid4().hex
        boot = self._boot_epoch()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            # Sample the clock only AFTER acquiring SQLite's writer lock.
            # A queued contender must not reclaim using a stale lease time.
            now = time.time()
            db.execute("""INSERT INTO hub_leader(singleton,owner,expires,lease_token,boot_epoch)
              VALUES(1,?,?,?,?)
              ON CONFLICT(singleton) DO UPDATE SET
                owner=excluded.owner,expires=excluded.expires,
                lease_token=CASE WHEN hub_leader.owner=excluded.owner
                    AND hub_leader.expires>=? AND hub_leader.lease_token<>''
                    AND (hub_leader.boot_epoch=0 OR ?=0
                         OR abs(hub_leader.boot_epoch-?)<=60)
                    THEN hub_leader.lease_token ELSE excluded.lease_token END,
                boot_epoch=CASE WHEN excluded.boot_epoch>0
                    THEN excluded.boot_epoch ELSE hub_leader.boot_epoch END
              WHERE hub_leader.expires < ? OR hub_leader.owner = ?
                 OR (hub_leader.boot_epoch>0 AND ?>0
                     AND abs(hub_leader.boot_epoch-?)>60)""",
              (owner, now + lifetime, candidate, boot,
               now, boot, boot, now, owner, boot, boot))
            claimed = db.execute(
                'SELECT owner,expires,lease_token,boot_epoch FROM hub_leader WHERE singleton=1'
            ).fetchone()
            db.commit()
        return bool(claimed and claimed[0] == owner
                    and claimed[1] >= now and claimed[2]
                    and self._same_boot(claimed[3], boot))

    def live_leader(self):
        """Only an unexpired leader from this OS boot can certify readiness.

        A SQLite file survives reboot while its owner process does not. A
        future wall-clock expiry from the previous boot must not prevent a
        new producer from starting, nor authenticate a stale readiness file.
        """
        with self._connect() as db:
            row = db.execute(
                'SELECT owner,expires,lease_token,boot_epoch FROM hub_leader '
                'WHERE singleton=1'
            ).fetchone()
        if (row and type(row[1]) in (int, float) and math.isfinite(row[1])
                and row[1] > time.time() and row[2]
                and self._same_boot(row[3], self._boot_epoch())):
            return row
        return None

    def leader_token(self, owner):
        """Opaque generation of the current unexpired same-boot lease."""
        with self._connect() as db:
            row = db.execute(
                'SELECT lease_token,boot_epoch FROM hub_leader WHERE singleton=1 '
                'AND owner=? AND expires>=? AND lease_token<>\'\'',
                (owner, time.time()),
            ).fetchone()
        return row[0] if row and self._same_boot(row[1], self._boot_epoch()) else None

    def renew_leader(self, owner, *, token, lifetime=120):
        # A stale process cannot renew an expired or previous-boot generation,
        # even if the wall clock makes its old expiry appear to be in the future.
        if (not owner or not isinstance(token, str) or not token
                or not 1 <= lifetime <= 120):
            raise ValueError('Invalid hub lease')
        boot = self._boot_epoch()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            now = time.time()
            cursor = db.execute(
                "UPDATE hub_leader SET expires=? WHERE singleton=1 "
                "AND owner=? AND expires>=? AND lease_token=? "
                "AND (boot_epoch=0 OR ?=0 OR abs(boot_epoch-?)<=60)",
                (now + lifetime, owner, now, token, boot, boot),
            )
            return cursor.rowcount == 1

    def release_leader(self, owner, *, token):
        # A delayed shutdown from an earlier epoch must not revoke the lease
        # now held by a successor with the same logical producer identity.
        if not owner or not isinstance(token, str) or not token:
            raise ValueError('Invalid hub lease')
        with self._connect() as db:
            cursor = db.execute(
                'DELETE FROM hub_leader WHERE singleton=1 '
                'AND owner=? AND lease_token=?',
                (owner, token),
            )
            return cursor.rowcount == 1

    def finish_owned(self, key, delay, owner, error='', *, token, subscription_token):
        """Acknowledge only while this producer still holds an unexpired lease.

        The ownership check and subscription update share one SQLite writer
        transaction so a replacement producer cannot be raced by an old one.
        """
        if (not isinstance(token, str) or not token
                or not isinstance(subscription_token, str) or not subscription_token):
            raise ValueError('Hub completion requires leader and subscription fencing tokens')
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            # Both leader and subscription expiry are checked against the
            # instant the SQLite writer lock is held, not queue-entry time.
            now = time.time()
            boot = self._boot_epoch()
            valid = db.execute(
                'SELECT 1 FROM hub_leader WHERE singleton=1 AND owner=? '
                'AND expires>=? AND lease_token=? '
                'AND (boot_epoch=0 OR ?=0 OR abs(boot_epoch-?)<=60)',
                (owner, now, token, boot, boot),
            ).fetchone()
            if not valid:
                db.rollback()
                return False
            # A revived/pruned-and-recreated subscriber has a new incarnation,
            # even when the same producer still owns the leader lease.
            updated = db.execute(
                'UPDATE hub_subscriptions SET next_due=?,last_error=? '
                'WHERE k=? AND subscription_token=? AND active_until>?',
                (now + delay, str(error)[:300], key, subscription_token, now),
            )
            db.commit()
        return updated.rowcount == 1

    @staticmethod
    def identity(url, params):
        import hashlib
        # Nonfinite values are not legal JSON; allowing NaN/Infinity into a
        # durable subscription can make two processes disagree on its identity.
        try:
            params_text = json.dumps(params, sort_keys=True, separators=(',', ':'), allow_nan=False)
            identity_text = json.dumps([url, params], sort_keys=True, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError('Hub params must contain finite JSON values') from exc
        key = hashlib.sha256(identity_text.encode()).hexdigest()
        return key, params_text

    def subscribe(self, url, params, *, lifetime=60, priority=False, force_due=False):
        if (type(lifetime) not in (int, float) or not math.isfinite(lifetime)
                or not 0 < lifetime <= 3600):
            raise ValueError('Hub subscription lifetime must be finite and positive')
        if type(priority) is not bool or type(force_due) is not bool:
            raise ValueError('Hub priority/request flag must be boolean')
        from .market import PublicData
        PublicData.validated_public_request(url, params)
        if force_due and not (
                url == 'https://api.bybit.com/v5/market/kline'
                and type(params) is dict and params.get('category') == 'linear'
                and params.get('interval') == '1'
                and type(params.get('limit')) is int):
            raise ValueError('Only validated Bybit historical candles may rearm on demand')
        key, encoded = self.identity(url, params)
        generation = uuid.uuid4().hex
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            # A subscription may expire while waiting on a fleet-wide writer.
            # Refresh its incarnation and TTL from transaction admission.
            now = time.time()
            # A new request after a parked (completed) snapshot must have a
            # fresh subscription fencing token. Otherwise the older in-flight
            # completion can overwrite this new demand with a 3600s deadline.
            # Do NOT rotate for every concurrent caller when next_due=0:
            # they already share one outstanding fetch and token churn could
            # starve acknowledgements indefinitely. The whole decision is
            # transactional with the next_due rearm.
            db.execute('''INSERT INTO hub_subscriptions(k,url,params,active_until,next_due,priority_until,subscription_token)
              VALUES(?,?,?,?,0,?,?) ON CONFLICT(k) DO UPDATE SET
              active_until=max(hub_subscriptions.active_until,excluded.active_until),
              subscription_token=CASE
                             WHEN (?=1 AND hub_subscriptions.next_due>?)
                             OR hub_subscriptions.active_until<=?
                             OR hub_subscriptions.params<>excluded.params
                             OR hub_subscriptions.url<>excluded.url
                             THEN excluded.subscription_token
                             ELSE hub_subscriptions.subscription_token END,
              priority_until=CASE WHEN hub_subscriptions.active_until<=?
                             THEN excluded.priority_until
                             ELSE max(hub_subscriptions.priority_until,excluded.priority_until) END,
              params=excluded.params,
              url=excluded.url,
              next_due=CASE WHEN ?=1 OR hub_subscriptions.active_until<=?
                            OR hub_subscriptions.params<>excluded.params
                            OR hub_subscriptions.url<>excluded.url
                            THEN 0 ELSE hub_subscriptions.next_due END,
              last_error=CASE WHEN hub_subscriptions.active_until<=?
                              OR hub_subscriptions.params<>excluded.params
                              OR hub_subscriptions.url<>excluded.url
                              THEN '' ELSE hub_subscriptions.last_error END''',
              (key, url, encoded, now + lifetime,
               now + min(lifetime * 0.75, 90) if priority else 0,
               generation, int(force_due), now, now, now,
               int(force_due), now, now))
        return key

    def request_once(self, url, params, *, lifetime=12, priority=False):
        """Rearm a historical REST fetch only when a consumer asks again.

        The producer parks the subscription after one successful snapshot.
        This explicit demand uses the same lease and validated public route;
        it cannot reopen an exhausted HTTP budget or a 403/451 policy fence.
        """
        return self.subscribe(url, params, lifetime=lifetime,
                              priority=priority, force_due=True)

    def prune_expired(self, *, retention=3600, batch=256):
        """Bounded garbage collection of abandoned market subscriptions.

        The grace interval protects subscribers across short restarts and
        clock adjustments. A bounded SQL batch avoids locking the SQLite
        writer for an unbounded fleet-size delete; repeated producer polls
        eventually reclaim the backlog. Never prune an active subscription.
        """
        if (type(retention) not in (int, float) or not math.isfinite(retention)
                or not 60 <= retention <= 86400
                or type(batch) is not int or not 1 <= batch <= 1024):
            raise ValueError('Invalid Hub retention or cleanup batch')
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            cutoff = time.time() - retention
            cursor = db.execute(
                'DELETE FROM hub_subscriptions WHERE k IN ('
                'SELECT k FROM hub_subscriptions WHERE active_until<? '
                'ORDER BY active_until,k LIMIT ?)',
                (cutoff, batch),
            )
            return cursor.rowcount

    def active_bybit_symbols(self):
        """Return active validated market WS subscriptions, independent of due.

        This is a read-only registry scan: no REST, no cache writes. It lets
        the single producer subscribe every requested symbol immediately
        instead of waiting for each symbol's scheduled HTTP-shaped poll.
        """
        ticker_url = "https://api.bybit.com/v5/market/tickers"
        book_url = "https://api.bybit.com/v5/market/orderbook"
        tickers, books = set(), set()
        with self._connect() as db:
            now = time.time()
            rows = db.execute(
                "SELECT url,params FROM hub_subscriptions "
                "WHERE active_until>? AND url IN (?,?)",
                (now, ticker_url, book_url),
            ).fetchall()
        from .market import PublicData
        for url, raw in rows:
            try:
                params = json.loads(raw)
                PublicData.validated_public_request(url, params)
                if (type(params) is not dict or
                        params.get("category") != "linear" or
                        type(params.get("symbol")) is not str):
                    continue
                if url == ticker_url and set(params) == {"category", "symbol"}:
                    tickers.add(params["symbol"])
                if (url == book_url and set(params) == {"category", "symbol", "limit"}
                        and type(params["limit"]) is int and params["limit"] == 50):
                    books.add(params["symbol"])
            except (ValueError, TypeError):
                continue
        return tickers, books

    def active_bybit_klines(self):
        """Only latest 1m tail requests; historical backfills remain REST."""
        url = "https://api.bybit.com/v5/market/kline"
        with self._connect() as db:
            rows = db.execute(
                "SELECT params FROM hub_subscriptions "
                "WHERE active_until>? AND url=?",
                (time.time(), url),
            ).fetchall()
        from .market import PublicData
        symbols = set()
        for (raw,) in rows:
            try:
                params = json.loads(raw)
                PublicData.validated_public_request(url, params)
                if (type(params) is dict
                        and set(params) == {"category", "symbol", "interval", "limit"}
                        and params["category"] == "linear"
                        and params["interval"] == "1"
                        and type(params["limit"]) is int and params["limit"] == 10):
                    symbols.add(params["symbol"])
            except (ValueError, TypeError):
                continue
        return symbols

    def active_polymarket_assets(self):
        """Read-only active public CLOB book subscriptions."""
        url = "https://clob.polymarket.com/book"
        with self._connect() as db:
            rows = db.execute(
                "SELECT params FROM hub_subscriptions "
                "WHERE active_until>? AND url=?",
                (time.time(), url),
            ).fetchall()
        from .market import PublicData
        from .ws_polymarket import _TOKEN
        assets = set()
        for (raw,) in rows:
            try:
                params = json.loads(raw)
                PublicData.validated_public_request(url, params)
                if (type(params) is dict and set(params) == {"token_id"}
                        and type(params["token_id"]) is str
                        and _TOKEN.fullmatch(params["token_id"])):
                    assets.add(params["token_id"])
            except (ValueError, TypeError):
                continue
        return assets

    def active_binance_symbols(self):
        url = "https://api.binance.com/api/v3/ticker/price"
        with self._connect() as db:
            rows = db.execute(
                "SELECT params FROM hub_subscriptions "
                "WHERE active_until>? AND url=?",
                (time.time(), url),
            ).fetchall()
        from .market import PublicData
        from .ws_binance import _SYMBOL
        symbols = set()
        for (raw,) in rows:
            try:
                params = json.loads(raw)
                PublicData.validated_public_request(url, params)
                if (type(params) is dict and set(params) == {"symbol"}
                        and type(params["symbol"]) is str
                        and _SYMBOL.fullmatch(params["symbol"])):
                    symbols.add(params["symbol"])
            except (ValueError, TypeError):
                continue
        return symbols

    def stream_demand(self):
        """Validated active event keys and short-lived owned-exposure priority."""
        from .market import PublicData
        from .ws_symbols import USDT_SYMBOL
        with self._connect() as db:
            now = time.time()
            rows = db.execute(
                "SELECT url,params,priority_until FROM hub_subscriptions WHERE active_until>?",
                (now,),
            ).fetchall()
        active, priority = set(), set()
        base = "https://api.bybit.com/v5/market/"
        for url, raw, urgent in rows:
            try:
                params = json.loads(raw)
                PublicData.validated_public_request(url, params)
                if not isinstance(params, dict):
                    continue
                key = None
                if params.get("category") == "linear" and USDT_SYMBOL.fullmatch(params.get("symbol", "")):
                    if url == base + "tickers" and set(params) == {"category", "symbol"}:
                        key = ("tickers", params["symbol"])
                    elif (url == base + "orderbook" and set(params) == {"category", "symbol", "limit"}
                          and params["limit"] == 50):
                        key = ("depth50", params["symbol"])
                    elif (url == base + "kline" and set(params) == {"category", "symbol", "interval", "limit"}
                          and params["interval"] == "1" and params["limit"] == 10):
                        key = ("kline1", params["symbol"])
                elif url == "https://clob.polymarket.com/book" and set(params) == {"token_id"}:
                    key = ("polybook", params["token_id"])
                elif (url == "https://api.binance.com/api/v3/ticker/price"
                      and set(params) == {"symbol"} and USDT_SYMBOL.fullmatch(params["symbol"])):
                    key = ("binance_trade", params["symbol"])
                if key is not None:
                    active.add(key)
                    if urgent > now:
                        priority.add(key)
            except (ValueError, TypeError, KeyError):
                continue
        return active, priority

    def has_active_subscriptions(self):
        """Read-only liveness probe for the independent producer lifecycle.

        Workers renew subscriptions during active reads. Stopped workers no
        longer renew, so the hub may retire after their leases expire.
        """
        with self._connect() as db:
            now = time.time()
            return db.execute(
                'SELECT 1 FROM hub_subscriptions WHERE active_until>? LIMIT 1',
                (now,),
            ).fetchone() is not None

    def due(self, limit=32, *, reserve_regular=0, exclude=(), include_priority=False,
            include_generation=False, skip_ws_routes=(), history_lane=None):
        # Urgent position marks first, reserve slots for scans.
        if history_lane is not None and type(history_lane) is not bool:
            raise ValueError("Invalid REST history lane selector")

        # Urgency belongs to a short renewable lease, not a permanent flag.
        if type(include_generation) is not bool:
            raise ValueError('Hub generation flag must be boolean')
        if type(limit) is not int or limit < 1:
            raise ValueError('Invalid due batch limit')
        if type(reserve_regular) is not int or not 0 <= reserve_regular < limit:
            raise ValueError('Invalid regular reservation')
        # Exclude already running requests in SQL, not after LIMIT. Otherwise
        # the same few due rows hide thousands of pending subscriptions and
        # cause duplicate fetches whenever a slow HTTP request is in flight.
        exclude = tuple(exclude)
        if any(type(key) is not str for key in exclude) or len(exclude) > 64:
            raise ValueError('Invalid in-flight Hub exclusion keys')
        skip_sql = (' AND k NOT IN (' + ','.join('?' for _ in exclude) + ')') if exclude else ''
        skip_ws_routes = tuple(skip_ws_routes)
        ws_routes = frozenset({
            "https://api.bybit.com/v5/market/tickers",
            "https://api.bybit.com/v5/market/orderbook",
            "https://api.bybit.com/v5/market/kline",
            "https://clob.polymarket.com/book",
            "https://api.binance.com/api/v3/ticker/price",
        })
        if any(type(url) is not str or url not in ws_routes
               for url in skip_ws_routes):
            raise ValueError("Invalid WebSocket queue route filter")
        if skip_ws_routes:
            # Broad Bybit ticker packs and REST depth200 are NOT covered by
            # symbol-specific WS streams. Exclude only the exact migrated
            # request shapes; keep unrelated REST safety/mark reads alive.
            clauses = []
            for route in skip_ws_routes:
                if route == "https://api.binance.com/api/v3/ticker/price":
                    shape = ("coalesce(json_type(params,'$.symbol')='text',0)")
                elif route == "https://clob.polymarket.com/book":
                    shape = ("coalesce(json_type(params,'$.token_id')='text',0)")
                elif route.endswith("/tickers"):
                    shape = ("coalesce(json_type(params,'$.symbol')='text',0) "
                             "AND json_extract(params,'$.category')='linear'")
                elif route.endswith("/kline"):
                    shape = ("coalesce(json_type(params,'$.symbol')='text',0) "
                             "AND json_extract(params,'$.category')='linear' "
                             "AND json_extract(params,'$.interval')='1' "
                             "AND json_extract(params,'$.limit')=10 "
                             "AND json_type(params,'$.end') IS NULL "
                             "AND json_type(params,'$.start') IS NULL")
                else:
                    shape = ("coalesce(json_type(params,'$.symbol')='text',0) "
                             "AND json_extract(params,'$.category')='linear' "
                             "AND json_extract(params,'$.limit')=50")
                clauses.append("NOT (url=? AND CASE WHEN json_valid(params) "
                               "THEN (" + shape + ") ELSE 0 END)")
            skip_sql += ' AND ' + ' AND '.join(clauses)
        if history_lane is not None:
            # Filter BEFORE LIMIT, not after fetching the head of the
            # combined queue. Only this fixed allowlisted route is separated.
            op = "=" if history_lane else "<>"
            skip_sql += " AND url" + op + "'https://api.bybit.com/v5/market/kline'"
        # Rank actual financial-risk metadata before executable price/book,
        # then held-position candles. Grouping book with risk as equal-priority
        # lets an already-backlogged quote queue consume the one reserved
        # physical lane when a NEW tier request arrives. That silently
        # defeats the financial reserve. This is ordering only: provider
        # quotas, leases and freshness checks remain authoritative.
        urgent_order = (
            " ORDER BY CASE "
            "WHEN url IN ("
            "'https://api.bybit.com/v5/market/risk-limit',"
            "'https://api.bybit.com/v5/market/instruments-info'"
            ") THEN 0 "
            "WHEN url IN ("
            "'https://api.bybit.com/v5/market/tickers',"
            "'https://api.bybit.com/v5/market/orderbook'"
            ") THEN 1 ELSE 2 END, next_due,k LIMIT ?"
        )
        with self._connect() as db:
            # All three selections must observe one WAL read snapshot.
            # Otherwise a concurrent priority promotion / rearm between
            # SELECTs can return the same key from both classes or through
            # the urgent OFFSET. BEGIN (not IMMEDIATE) never takes the
            # SQLite writer reservation; end the read before validation.
            db.execute('BEGIN')
            now = time.time()
            urgent = db.execute('''SELECT k,url,params,subscription_token FROM hub_subscriptions
              WHERE active_until>? AND next_due<=? AND priority_until>?''' + skip_sql +
              urgent_order, (now, now, now, *exclude, *skip_ws_routes, limit-reserve_regular)).fetchall()
            regular = db.execute('''SELECT k,url,params,subscription_token FROM hub_subscriptions
              WHERE active_until>? AND next_due<=? AND priority_until<=?''' + skip_sql +
              ' ORDER BY next_due,k LIMIT ?', (now, now, now, *exclude, *skip_ws_routes, limit-len(urgent))).fetchall()
            rows = [(row, True) for row in urgent] + [(row, False) for row in regular]
            if len(rows) < limit and len(urgent) == limit-reserve_regular:
                more = db.execute('''SELECT k,url,params,subscription_token FROM hub_subscriptions
                  WHERE active_until>? AND next_due<=? AND priority_until>?''' + skip_sql +
                  urgent_order + ' OFFSET ?',
                  (now,now,now,*exclude,*skip_ws_routes,limit-len(rows),len(urgent))).fetchall()
                rows.extend((row, True) for row in more)
        # Older Hub databases can contain subscriptions admitted before the
        # shared public-route policy existed. Revalidate at dequeue as well as
        # at registration; invalid rows must not consume producer HTTP slots.
        from .market import PublicData
        valid = []
        for (key, url, raw_params, generation), is_urgent in rows:
            try:
                params = json.loads(raw_params, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
                PublicData.validated_public_request(url, params)
                if self.identity(url, params)[0] != key or not generation:
                    raise ValueError('Subscription identity or generation differs from persisted data')
            except (ValueError, TypeError, UnicodeError):
                with self._connect() as db:
                    db.execute('UPDATE hub_subscriptions SET last_error=?, next_due=? '
                               'WHERE k=? AND params=? AND subscription_token=?',
                               ('Invalid persisted public subscription',
                                time.time() + 60, key, raw_params, generation))
                continue
            row = (key, url, params)
            if include_priority:
                row += (is_urgent,)
            if include_generation:
                row += (generation,)
            valid.append(row)
        return valid

    def finish(self, key, delay, error=''):
        """Legacy unfenced completion is deliberately unavailable.

        Only finish_owned may mutate producer acknowledgements. Otherwise an
        obsolete worker or producer could bypass the lease-generation barrier.
        """
        raise RuntimeError('Unfenced Market Data Hub completion is forbidden')


class MarketDataHub:
    """Single producer intended to run as one managed service process."""
    def __init__(self, cache_path, source=None, *, max_workers=1):
        from .market import PublicData
        if type(max_workers) is not int or max_workers < 1:
            raise ValueError('Hub producer concurrency must be a positive integer')
        # Provider request quotas remain enforced separately; the operator's
        # concurrency setting is a resource budget, not a fixed software cap.
        self.max_workers = max_workers
        # One bounded pool for the whole lifetime of this independent Hub.
        # A new pool per poll would constantly churn threads on large scans.
        self._pool = (ThreadPoolExecutor(max_workers=max_workers,
                          thread_name_prefix='hub-public-get') if max_workers > 1 else None)
        self._history_pool = None
        self.registry = HubRegistry(cache_path)
        self.owner = f"{os.getpid()}:{uuid.uuid4().hex}"
        # Steady-state producer: never hold the entire batch hostage to the
        # slowest HTTP request. Futures remain in flight between short polls.
        self._inflight = {}
        self._leader_check_due = 0.0
        self._leader_held = False
        self._leader_token = None
        self._prune_due = 0.0
        self._ws_subscription_scan_due = 0.0
        self._ws_mark_scan_due = 0.0
        self._ticker_demand_due = 0.0
        self._derived_ticker_renewals = 0
        self._ws_quote_symbols = ()
        self._ws_book_symbols = ()
        self._ws_dispatcher = None
        self._finalized_published = {}
        self._repair_boundary = None
        self._repair_seen = set()
        self._repair_scheduled = 0
        self._repair_completed = 0
        # WS -> SQLite publication evidence. Aggregate only: no symbols,
        # prices, account details, or exchange timestamp modifications.
        self._ws_publication_lock = threading.Lock()
        self._ws_publication_stats = dict(
            batches=0, events_seen=0, prepared=0, published=0,
            rejected_source=0, rejected_leader=0, rejected_store=0,
            sqlite_busy=0, unexpected_errors=0,
            max_prepare_seconds=0.0, max_commit_seconds=0.0,
            max_batch_seconds=0.0,
        )
        self.lease_renew_interval = 5.0
        self.source = source or PublicData()
        if self.source.shared is None:
            self.source.shared = PublicCache(cache_path)

    def enable_split_history(self):
        """Activate dedicated REST executors before admitting producer work."""
        if self._history_pool is not None or self._inflight or not 3 <= self.max_workers <= 64:
            raise ValueError("Split REST lanes require a new Hub with 3..64 workers")
        self._history_pool = ThreadPoolExecutor(
            max_workers=self.max_workers // 2, thread_name_prefix="hub-history-get")
        self.source.shared.split_history_lanes = True

    def rest_lane_health(self):
        if self._history_pool is None:
            return {"enabled": False}
        from .hub_rest_lanes import KLINE_URL
        rows = list(self._inflight.values())
        count = sum(item[1] == KLINE_URL for item in rows)
        return {"enabled": True,
                "history_capacity": self.max_workers // 2,
                "metadata_capacity": self.max_workers - self.max_workers // 2,
                "history_inflight": count,
                "metadata_inflight": len(rows) - count,
                "ack_pending": sum(item[0].done() for item in rows)}

    def acquire_leadership(self, *, lifetime=120):
        """Single admission path for service readiness and every producer mode.

        The token must be retained before the service announces readiness,
        otherwise an early shutdown cannot release its own lease safely.
        """
        if not self.registry.claim_leader(self.owner, lifetime=lifetime):
            self._leader_token = None
            self._leader_held = False
            return None
        token = self.registry.leader_token(self.owner)
        self._leader_token = token
        self._leader_held = bool(token)
        return token

    def _maintain_registry(self):
        """Only the admitted leader performs bounded subscription cleanup."""
        now = time.monotonic()
        if now >= self._prune_due:
            self.registry.prune_expired()
            self._prune_due = now + 30.0

    def _prepare_ws_event(self, kind, symbol):
        """Capture independently timed stream evidence without network or SQL."""
        from .ws_quotes import StreamUnavailable
        from .market_clock import market_now
        if not self._leader_held or not self._leader_token:
            return False
        if kind == "tickers":
            stream = getattr(self.source, "_quote_stream", None)
            url = "https://api.bybit.com/v5/market/tickers"
            params = {"category": "linear", "symbol": symbol}
        elif kind == "depth50":
            stream = getattr(self.source, "_orderbook_stream", None)
            url = "https://api.bybit.com/v5/market/orderbook"
            params = {"category": "linear", "symbol": symbol, "limit": 50}
        elif kind == "binance_trade":
            stream = getattr(self.source, "_binance_stream", None)
            url = "https://api.binance.com/api/v3/ticker/price"
            params = {"symbol": symbol}
        elif kind == "polybook":
            stream = getattr(self.source, "_polymarket_stream", None)
            url = "https://clob.polymarket.com/book"
            params = {"token_id": symbol}
        elif kind == "kline1":
            stream = getattr(self.source, "_kline_stream", None)
            url = "https://api.bybit.com/v5/market/kline"
            params = {"category": "linear", "symbol": symbol, "interval": "1", "limit": 10}
        else:
            raise ValueError("unknown WS publication channel")
        if stream is None:
            return False
        try:
            if kind == "binance_trade":
                price, at = stream.quote(symbol)
                if (not math.isfinite(price) or price <= 0
                        or not -1 <= market_now() - at <= 2):
                    return False
                value = {
                    "symbol": symbol, "price": str(price),
                    "_ws_timestamp": at, "_bcc_ws_binance": True,
                }
                ttl = .35
            elif kind == "polybook":
                book = stream.get_book(symbol)
                stamp = float(book["timestamp"]) / 1000
                if not -1 <= market_now() - stamp <= 2:
                    return False
                value = {"_bcc_ws_polybook": True, **book}
                ttl = .35
            elif (kind == "tickers" and getattr(self.source.shared, "bybit_ws_source_deadlines", False)
                  and callable(getattr(stream, "independent_observations_for_hub", None))):
                from .ws_split_ticker import make_split_ticker, project_ticker
                mark, quote = stream.independent_observations_for_hub(symbol)
                value, _ = project_ticker(make_split_ticker(symbol, mark, quote), symbol=symbol)
                if value is None:
                    if not getattr(stream, 'maintained_enabled', False):
                        return False
                    value = make_split_ticker(symbol)  # revoke, never publish a price
                ttl = 1.5
            elif kind == "tickers":
                # Independently transport the exchange's real markPrice.
                # Partial ticker deltas may refresh mark without changing
                # bid/ask. Never synthesize a mark from an executable spread.
                mark = mark_at = bid = ask = at = None
                connection_fence = None
                atomic = getattr(stream, "snapshot_for_hub", None)
                if callable(atomic):
                    # Both independent exchange observations belong to one
                    # bounded WS generation, including across reconnects.
                    observed_mark, observed_quote, connection_fence = atomic(symbol)
                    if observed_mark is not None:
                        mark, mark_at = observed_mark
                    if observed_quote is not None:
                        bid, ask, at = observed_quote
                else:
                    # Compatible third-party adapters cannot carry marks
                    # across publication without a generation fence.
                    if hasattr(stream, "mark"):
                        try:
                            mark, mark_at = stream.mark(symbol)
                        except StreamUnavailable:
                            pass
                    try:
                        bid, ask, at = stream.quote(symbol)
                    except StreamUnavailable:
                        pass
                if mark is None and bid is None:
                    return False
                if (mark is not None and
                    (not math.isfinite(mark) or mark <= 0
                     or not math.isfinite(mark_at)
                     or not -0.5 <= market_now() - mark_at <= 3)):
                    return False
                if (bid is not None and
                    (not all(math.isfinite(x) for x in (bid, ask, at))
                     or bid <= 0 or ask <= 0 or bid > ask
                     or not -5 <= market_now() - at <= 2)):
                    return False
                row = {"symbol": symbol}
                if bid is not None:
                    row.update(bid1Price=str(bid), ask1Price=str(ask))
                if mark is not None:
                    row["markPrice"] = str(mark)
                # Never attribute the bid/ask event time to an older mark.
                # Both public WS observations retain separate exchange clocks.
                value = {
                    "retCode": 0, "retMsg": "OK",
                    "_bcc_ws_quote": bid is not None,
                    "_bcc_ws_mark": mark is not None,
                    "_bcc_ws_connection_fence": connection_fence,
                    "_bcc_ws_mark_at": int(mark_at * 1000) if mark is not None else None,
                    "time": int((at if bid is not None else mark_at) * 1000),
                    "result": {"list": [row]},
                }
                # Bridge genuine one-second recovery snapshots without
                # discarding otherwise fresh independent quote/mark clocks.
                ttl = 1.5
            elif kind == "kline1":
                final_reader = getattr(stream, "get_confirmed_candles", None)
                closed = (final_reader(symbol)
                          if getattr(self.source.shared, "central_closed_history", False)
                          and callable(final_reader) else [])
                candle = None
                try:
                    current = stream.get_candle(symbol)
                    if -5 <= market_now() - current.message_ms / 1000 <= 10:
                        candle = current
                except StreamUnavailable:
                    pass
                if candle is None and not closed:
                    return False
                if candle is None:
                    # Historical evidence must not republish an expired
                    # forming candle. Only the separate minute key is written.
                    value = {"retCode": 0, "_bcc_ws_kline": True,
                             "_bcc_ws_closed_only": True,
                             "time": max(row["confirmed_ms"] for row in closed),
                             "result": {"list": []}}
                else:
                    value = {
                        "retCode": 0, "retMsg": "OK", "_bcc_ws_kline": True,
                        "_bcc_ws_kline_confirmed": getattr(candle, "confirmed", False) is True,
                        "time": candle.message_ms,
                        "result": {"list": [[
                            str(candle.start_ms), str(candle.open), str(candle.high),
                            str(candle.low), str(candle.close), str(candle.volume),
                            "0",
                        ]]},
                    }
                if closed:
                    value["_bcc_ws_finalized_minutes"] = closed
                ttl = .75
            else:
                book = stream.get_orderbook(symbol)
                stamp = book["ts"]
                if (not math.isfinite(stamp) or
                        not -.5 <= market_now() - stamp <= 3):
                    return False
                value = {
                    "retCode": 0, "retMsg": "OK", "_bcc_ws_book": True,
                    "time": int(stamp * 1000),
                    "result": {
                        "s": symbol, "ts": int(stamp * 1000),
                        "b": [[str(p), str(q)] for p, q in book["bids"]],
                        "a": [[str(p), str(q)] for p, q in book["asks"]],
                        "u": book["u"], "seq": book["seq"],
                    },
                }
                # Quiet depth recovery runs at one-second cadence. Cache
                # residence bridges it; ORIGINAL exchange timestamps still
                # face the strict three-second worker/executor age fence.
                ttl = 1.5
        except (StreamUnavailable, ValueError, KeyError, TypeError):
            return False
        return dict(url=url, params=params,
                    host=("clob.polymarket.com" if kind == "polybook"
                          else "api.binance.com" if kind == "binance_trade"
                          else "api.bybit.com"), value=value, ttl=ttl)

    def publish_ws_event(self, kind, symbol):
        return self.publish_ws_events([(kind, symbol)])[0]

    def ws_publication_stats(self):
        """Aggregate WS admission and SQLite persistence; no market payloads."""
        with self._ws_publication_lock:
            return dict(self._ws_publication_stats)

    def publish_ws_events(self, events):
        """Publish genuine independent WS snapshots and classify failures.

        Instrumentation cannot extend cache TTL, alter original exchange
        timestamps, change subscription admission or bypass the leader lease.
        """
        if type(events) not in (list, tuple) or len(events) > 64:
            raise ValueError("Invalid WS event batch")
        start = time.monotonic()
        valid = []
        results = [False] * len(events)
        prepare_seconds = commit_seconds = 0.0
        busy = unexpected = rejected_leader = 0
        try:
            token = self._leader_token
            prepared = [self._prepare_ws_event(kind, symbol) for kind, symbol in events]
            valid = [(index, row) for index, row in enumerate(prepared) if row]
            prepare_seconds = time.monotonic() - start
            if valid and (not token or token != self._leader_token):
                rejected_leader = len(valid)
            elif valid:
                from .data_cache import CacheBusy
                commit_start = time.monotonic()
                try:
                    outcomes = self.source.shared.publish_websockets(
                        [row for _, row in valid], leader_owner=self.owner,
                        leader_token=token)
                except CacheBusy as exc:
                    busy = len(valid)
                    from .data_cache import CacheContention
                    if (getattr(self.source.shared, "central_closed_history", False)
                            and isinstance(exc, CacheContention) and self._ws_dispatcher is not None):
                        # Re-read current exchange evidence on retry. Never
                        # replay stale payloads or extend original prices.
                        for index, _ in valid:
                            self._ws_dispatcher.notify(*events[index])
                else:
                    for (index, record), outcome in zip(valid, outcomes):
                        results[index] = outcome
                        rows = record["value"].get("_bcc_ws_finalized_minutes")
                        if outcome and rows:
                            self._finalized_published[events[index][1]] = tuple(
                                (row["t"], row["confirmed_ms"]) for row in rows)
                finally:
                    commit_seconds = time.monotonic() - commit_start
            return results
        except BaseException:
            unexpected = 1
            raise
        finally:
            successes = sum(outcome is True for outcome in results)
            with self._ws_publication_lock:
                s = self._ws_publication_stats
                s['batches'] += 1
                s['events_seen'] += len(events)
                s['prepared'] += len(valid)
                s['published'] += successes
                if not unexpected:
                    s['rejected_source'] += len(events) - len(valid)
                s['rejected_leader'] += rejected_leader
                s['rejected_store'] += len(valid) - successes - rejected_leader
                s['sqlite_busy'] += busy
                s['unexpected_errors'] += unexpected
                s['max_prepare_seconds'] = round(max(
                    s['max_prepare_seconds'], prepare_seconds), 6)
                s['max_commit_seconds'] = round(max(
                    s['max_commit_seconds'], commit_seconds), 6)
                s['max_batch_seconds'] = round(max(
                    s['max_batch_seconds'], time.monotonic() - start), 6)

    def _websocket_queue_exclusions(self):
        routes = []
        if getattr(self.source, "_quote_stream", None) is not None:
            routes.append("https://api.bybit.com/v5/market/tickers")
        if getattr(self.source, "_orderbook_stream", None) is not None:
            routes.append("https://api.bybit.com/v5/market/orderbook")
        if getattr(self.source, "_kline_stream", None) is not None:
            routes.append("https://api.bybit.com/v5/market/kline")
        if getattr(self.source, "_polymarket_stream", None) is not None:
            routes.append("https://clob.polymarket.com/book")
        if getattr(self.source, "_binance_stream", None) is not None:
            routes.append("https://api.binance.com/api/v3/ticker/price")
        return tuple(routes)

    def set_ws_dispatcher(self, dispatcher):
        self._ws_dispatcher = dispatcher
        self._ws_subscription_scan_due = 0.

    def _schedule_minute_repairs(self, symbols):
        """One proactive, canonical REST request per missing closed minute.

        The normal REST history pool executes the request with its original
        provider budget, denial and HTTP ownership checks. No direct network
        operation is performed on the WS subscription/control thread.
        """
        from .market_clock import ClockUnavailable, market_now
        from .hub_minute_repair import schedule
        try:
            now_ms = int(market_now()*1000)
        except ClockUnavailable:
            return  # No verified minute; leave all outstanding demand intact.
        boundary = now_ms//60000*60000
        if now_ms-boundary < 1500:
            return
        if self._repair_boundary != boundary:
            self._repair_boundary = boundary
            self._repair_seen.clear()
        pending = sorted(set(symbols)-self._repair_seen)
        if pending:
            from .data_cache import CacheContention
            try:
                self.source.shared._ensure_schema()
            except CacheContention:
                return  # Retry subscription scan; no market evidence admitted.
        for offset in range(0,len(pending),64):
            jobs = pending[offset:offset+64]
            with self.registry._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    now_ms = int(market_now()*1000)
                except ClockUnavailable:
                    db.rollback()
                    return
                if now_ms//60000*60000 != boundary:
                    db.rollback()
                    return
                # Refusals are never bypassed, including by scheduling.
                from .data_cache import PublicCache
                if PublicCache._denied(db,"api.bybit.com") is not None:
                    return
                added = schedule(db,jobs,boundary-60000,time.time(),now_ms,
                    owner=self.owner,token=self._leader_token,boot=self.registry._boot_epoch())
                if added is None:
                    return
                db.commit()
            self._repair_seen.update(jobs)
            self._repair_scheduled += len(added)

    def _renew_ticker_demand_from_history(self):
        """Lease-fenced batch preparation of marks for already active history."""
        from .ws_ticker_demand import renew_tickers
        from .data_cache import PublicCache, CacheBusy
        if self._leader_token is None:
            return 0
        try:
            self.source.shared._ensure_schema()
            with self.registry._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                if PublicCache._denied(db,"api.bybit.com") is not None:
                    return 0
                changed = renew_tickers(db,now=time.time(),owner=self.owner,
                    token=self._leader_token,boot=self.registry._boot_epoch(),
                    identity=self.registry.identity)
                if changed is None:
                    return 0
                db.commit()
            self._derived_ticker_renewals += changed
            return changed
        except CacheBusy:
            return 0
        except sqlite3.OperationalError as exc:
            if any(word in str(exc).lower() for word in ("locked","busy")):
                return 0
            raise

    def subscribe_active_websockets(self):
        """Pre-register every demanded symbol on the shared WS producers."""
        if self._leader_token is None:
            return
        now = time.monotonic()
        ticker = getattr(self.source, "_quote_stream", None)
        orderbook = getattr(self.source, "_orderbook_stream", None)
        # Recovery of quiet prices uses cached demand at a shorter cadence
        # than SQLite discovery. Both marks and depth require genuine events.
        if now >= self._ws_mark_scan_due:
            self._ws_mark_scan_due = now + .25
            from .ws_quotes import StreamUnavailable
            for stream, symbols, method in (
                    (ticker, self._ws_quote_symbols, "refresh_stale_marks"),
                    (orderbook, self._ws_book_symbols, "refresh_stale_books")):
                refresh = getattr(stream, method, None)
                if symbols and callable(refresh):
                    try:
                        refresh(symbols)
                    except (ValueError, StreamUnavailable):
                        pass
        if now < self._ws_subscription_scan_due:
            return
        self._ws_subscription_scan_due = now + 1.0
        if (ticker is not None and os.environ.get("BCC_BYBIT_SHARDED_TICKERS_CANDIDATE")=="1"
                and now>=self._ticker_demand_due):
            self._ticker_demand_due = now + 10.
            self._renew_ticker_demand_from_history()
        prioritize_marks = getattr(ticker, "set_priority_symbols", None)
        prioritize_books = getattr(orderbook, "set_priority_symbols", None)
        if (self._ws_dispatcher is not None or callable(prioritize_marks)
                or callable(prioritize_books)):
            active, priority = self.registry.stream_demand()
            if self._ws_dispatcher is not None:
                self._ws_dispatcher.configure(active, priority)
            if callable(prioritize_marks):
                prioritize_marks([symbol for kind, symbol in priority if kind == "tickers"])
            if callable(prioritize_books):
                prioritize_books([symbol for kind, symbol in priority if kind == "depth50"])
        ticker = getattr(self.source, "_quote_stream", None)
        orderbook = getattr(self.source, "_orderbook_stream", None)
        kline = getattr(self.source, "_kline_stream", None)
        poly = getattr(self.source, "_polymarket_stream", None)
        binance = getattr(self.source, "_binance_stream", None)
        if (ticker is None and orderbook is None and kline is None
                and poly is None and binance is None):
            return
        tickers, books = self.registry.active_bybit_symbols()
        if ticker is not None:
            # One outbound subscription command per ten symbols rather than
            # one message per coin in the fleet.
            validated = []
            from .ws_quotes import _SYMBOL
            for symbol in sorted(tickers):
                if _SYMBOL.fullmatch(symbol):
                    validated.append(symbol)
            self._ws_quote_symbols = tuple(validated)
            self._ws_mark_scan_due = now + .25
            from .ws_quotes import StreamUnavailable
            try:
                if hasattr(ticker, "sync_symbols"):
                    ticker.sync_symbols(validated)
                elif hasattr(ticker, "subscribe_many"):
                    ticker.subscribe_many(validated)
                else:
                    for symbol in validated:
                        ticker.subscribe(symbol)
                # Quiet symbols may not emit a markPrice delta within the
                # strict collateral TTL. Recover with new *exchange* ticker
                # snapshots, never by extending cached timestamps or by
                # presenting executable spread as the missing mark.
                if hasattr(ticker, "refresh_stale_marks"):
                    ticker.refresh_stale_marks(validated)
            except (ValueError, StreamUnavailable):
                # Only this feed reconnects. Other providers remain alive.
                pass
        if orderbook is not None:
            from .ws_quotes import StreamUnavailable
            valid_books = []
            from .ws_quotes import _SYMBOL
            for symbol in sorted(books):
                if _SYMBOL.fullmatch(symbol):
                    valid_books.append(symbol)
            self._ws_book_symbols = tuple(valid_books)
            try:
                if hasattr(orderbook, "sync_symbols"):
                    orderbook.sync_symbols(valid_books)
                else:
                    orderbook.subscribe(valid_books)
                if hasattr(orderbook, "refresh_stale_books"):
                    orderbook.refresh_stale_books(valid_books)
            except (ValueError, StreamUnavailable):
                pass
        if kline is not None:
            valid_klines = []
            from .ws_quotes import _SYMBOL
            for symbol in sorted(self.registry.active_bybit_klines()):
                if _SYMBOL.fullmatch(symbol):
                    valid_klines.append(symbol)
            try:
                if hasattr(kline, "sync_symbols"):
                    kline.sync_symbols(valid_klines)
                else:
                    kline.subscribe(valid_klines)
                # A quiet symbol may send its final message just before the
                # conservative local closing boundary. Revisit uncommitted
                # final evidence during the existing subscription scan.
                reader = getattr(kline, "get_confirmed_candles", None)
                if (getattr(self.source.shared, "central_closed_history", False)
                        and self._ws_dispatcher is not None and callable(reader)):
                    active = set(valid_klines)
                    for old in tuple(self._finalized_published):
                        if old not in active:
                            self._finalized_published.pop(old, None)
                    for symbol in valid_klines:
                        rows = reader(symbol)
                        identity = tuple((row["t"], row["confirmed_ms"]) for row in rows)
                        if identity and identity != self._finalized_published.get(symbol):
                            self._ws_dispatcher.notify("kline1", symbol)
            except (ValueError, StreamUnavailable):
                pass
            if getattr(self.source.shared,"central_repair",False):
                self._schedule_minute_repairs(valid_klines)
        if binance is not None:
            try:
                binance.subscribe(sorted(self.registry.active_binance_symbols()))
            except (ValueError, StreamUnavailable):
                pass
        if poly is not None:
            try:
                desired = sorted(self.registry.active_polymarket_assets())
                if hasattr(poly, "sync_assets"):
                    poly.sync_assets(desired)
                else:
                    poly.subscribe(desired)
            except (ValueError, StreamUnavailable):
                pass

    def _fetch_public(self, url, params, *, urgent):
        """Preserve subscriber priority across ALL Hub producer modes.

        PublicData checks its thread-local position_mark before reserving the
        shared Bybit request budget. A priority queue alone cannot protect
        open PAPER positions if the producer drops that mark at the HTTP
        boundary. Always restore the worker-thread context, including errors.
        """
        context = getattr(self.source, '_thread', None)
        previous = getattr(context, 'position_mark', False) if context is not None else False
        # The Hub queue has two urgent classes, not one HTTP privilege.
        # Held-position kline history may outrank cold scans in scheduling,
        # but it MUST NOT consume PublicCache's reserved Bybit financial
        # mark/risk request budget. A history request is never a mark.
        financial_priority = bool(
            urgent and url != 'https://api.bybit.com/v5/market/kline'
        )
        if context is not None:
            context.position_mark = financial_priority
        try:
            result = self.source.get(url, params=params)
            if (getattr(self.source.shared,"central_repair",False)
                    and url=="https://api.bybit.com/v5/market/kline"
                    and type(params) is dict and params.get("interval")=="1"
                    and params.get("limit")==1 and type(params.get("end")) is int
                    and (params["end"]+1)%60000==0):
                from .market_clock import market_now
                from .hub_minute_repair import rest_minute
                if rest_minute(result,params["symbol"],params["end"]-59999,
                               int(market_now()*1000)) is not None:
                    with self._ws_publication_lock:
                        self._repair_completed += 1
            return result
        finally:
            if context is not None:
                context.position_mark = previous

    def _acknowledge(self, key, delay, error, *, token, generation):
        """Classify a fenced acknowledgement without confusing subscriber churn
        with loss of producer leadership.

        A subscriber can expire, be pruned or re-register under a new
        incarnation while its old HTTP request is in flight. Its stale
        completion must be discarded, but the same leader must continue
        servicing unrelated subscriptions. Only actual leadership loss stops
        the producer. Every successful acknowledgement is still checked in
        the same SQLite transaction as the two fencing tokens.
        """
        if self.registry.finish_owned(
                key, delay, self.owner, error, token=token,
                subscription_token=generation):
            return "acknowledged"
        if self.registry.leader_token(self.owner) == token:
            return "stale_subscription"
        return "lost_leader"

    def _refresh_delay(self, url, params, *, failed=False):
        # A complete historical GET is a one-shot response to a real caller,
        # not a perpetual one-second poll. Re-arm only from a NEW client
        # request_once(). Failed requests retain the ordinary bounded retry
        # while the client is still waiting and the lease is active.
        if (not failed and url == 'https://api.bybit.com/v5/market/kline'
                and type(params) is dict and params.get('category') == 'linear'
                and params.get('interval') == '1'):
            return 3600.
        delay = 2. if failed else 120. if '/risk-limit' in url else .25 if 'orderbook' in url or '/book' in url else 1.
        cap = delay
        if not failed and self._history_pool is not None:
            # Only an existing cache row proves a long metadata deadline.
            # Missing rows keep the original short retry interval.
            if '/instruments-info' in url:
                cap = 3600.
            elif '/funding/history' in url:
                cap = 60.
        remaining = getattr(getattr(self.source,'shared',None),'remaining_ttl',None)
        if failed or not callable(remaining):
            return delay
        from .data_cache import CacheBusy, ProviderDenied
        from .market import PublicData
        try:
            ttl = remaining(url,params,PublicData.validated_public_request(url,params))
            if ttl is None:
                return delay  # Legacy/custom adapters may not publish a row.
            # Rechecking cached risk data must not postpone its real renewal
            # for another whole poll interval after that cache has expired.
            return max(.05,min(cap,ttl+.025))
        except (CacheBusy,ProviderDenied):
            return min(delay,2.)

    def run_once(self, limit=32):
        if self.max_workers > 1:
            return self._run_parallel(limit)
        token = self.acquire_leadership()
        if not token:
            return []
        self._maintain_registry()
        self.subscribe_active_websockets()
        results = []
        for key, url, params, urgent, generation in self.registry.due(
                limit, include_priority=True, include_generation=True,
                skip_ws_routes=self._websocket_queue_exclusions()):
            if not self.registry.renew_leader(self.owner, token=token, lifetime=120):
                break
            stop_renewal = threading.Event()
            lease_lost = threading.Event()

            def keep_lease():
                # The producer is not idle while an HTTP request is in flight.
                # Another hub must not take ownership just because HTTP is slow.
                while not stop_renewal.wait(self.lease_renew_interval):
                    try:
                        if not self.registry.renew_leader(self.owner, token=token, lifetime=120):
                            lease_lost.set()
                            return
                    except sqlite3.Error:
                        # Failure to renew means we can no longer prove ownership.
                        lease_lost.set()
                        return

            heartbeat = threading.Thread(target=keep_lease, name='hub-lease-renewal', daemon=True)
            heartbeat.start()
            error = None
            try:
                self._fetch_public(url, params, urgent=urgent)
            except Exception as exc:
                error = exc
            finally:
                stop_renewal.set()
                heartbeat.join(timeout=2)
            # A stale producer must not publish subscription completion after
            # ownership transfer. Cache writes inside source.get are protected
            # separately by PublicCache's per-request ownership lease.
            if lease_lost.is_set() or not self.registry.renew_leader(self.owner, token=token, lifetime=120):
                break
            outcome = self._acknowledge(
                key, self._refresh_delay(url,params,failed=error is not None),
                str(error) if error is not None else "",
                token=token, generation=generation,
            )
            if outcome == "lost_leader":
                break
            if outcome == "acknowledged":
                results.append((key, error is None))
        return results

    def _run_parallel(self, limit):
        """Bounded producer parallelism with a single lease owner.

        Exchange budgets and per-URL singleflight remain centralized in
        PublicCache. No worker writes accounting state; only the leader main
        thread acknowledges completed subscriptions. Fetch concurrency is
        bounded independently of the much larger registered symbol count.
        """
        token = self.acquire_leadership()
        if not token:
            return []
        self._maintain_registry()
        self.subscribe_active_websockets()
        batch = min(limit, self.max_workers)
        # A one-request batch has no second lane to reserve. Keep the
        # scheduler's reservation strictly below the actual batch size.
        due = self.registry.due(batch,
                                reserve_regular=1 if batch > 1 else 0,
                                include_priority=True, include_generation=True,
                                skip_ws_routes=self._websocket_queue_exclusions())
        if not due:
            return []
        stop = threading.Event()
        lost = threading.Event()

        def keep_leader():
            while not stop.wait(self.lease_renew_interval):
                try:
                    if not self.registry.renew_leader(self.owner, token=token, lifetime=120):
                        lost.set()
                        return
                except sqlite3.Error:
                    lost.set()
                    return

        def fetch(request):
            key, url, params, urgent, generation = request
            try:
                self._fetch_public(url, params, urgent=urgent)
                return key, url, params, generation, None
            except Exception as exc:
                return key, url, params, generation, exc

        keeper = threading.Thread(target=keep_leader, name='hub-lease-renewal', daemon=True)
        keeper.start()
        results = []
        try:
            futures = [self._pool.submit(fetch, request) for request in due]
            for future in as_completed(futures):
                key, url, params, generation, error = future.result()
                if lost.is_set() or not self.registry.renew_leader(self.owner, token=token, lifetime=120):
                    lost.set()
                    break
                outcome = self._acknowledge(
                    key, self._refresh_delay(url,params,failed=error is not None), str(error) if error is not None else "",
                    token=token, generation=generation,
                )
                if outcome == "lost_leader":
                    lost.set()
                    break
                if outcome == "acknowledged":
                    results.append((key, error is None))
        finally:
            stop.set()
            keeper.join(timeout=2)
        return results

    def poll_nonblocking(self, *, market_ready=True):
        """Run one short producer scheduling turn without awaiting slow HTTP.

        A slow regular request must not delay urgent marks on free workers.
        Leader ownership is renewed on a bounded cadence, and every completed
        acknowledgement is fenced by the authoritative SQLite leader lease.
        Only the Hub service uses this method; run_once stays synchronous for
        deterministic offline tests and existing integrations.
        """
        if self._pool is None and market_ready:
            return self.run_once()
        now = time.monotonic()
        if now >= self._leader_check_due:
            held = (
                self.registry.renew_leader(self.owner, token=self._leader_token, lifetime=120)
                if self._leader_held
                else self.acquire_leadership()
            )
            self._leader_token = self.registry.leader_token(self.owner) if held else None
            self._leader_held = bool(self._leader_token)
            self._leader_check_due = now + 1.0
        if not self._leader_held:
            # Do not publish from a superseded producer. Running HTTP cannot
            # always be cancelled; its single-flight cache lease is separate.
            return []

        # Keep the leadership lease alive during a clock outage. Do not
        # admit new market work, acknowledge old futures, or invent a time
        # boundary. Completed HTTP jobs remain accounted for until recovery.
        if not market_ready:
            return []
        self._maintain_registry()
        self.subscribe_active_websockets()
        results = []
        for key, (future, url, is_urgent, launched_token, generation, params) in list(self._inflight.items()):
            if not future.done():
                continue
            # A task launched under an earlier lease must not acknowledge
            # subscriptions after the producer loses and regains leadership.
            # Keep it accounted for until completion to bound HTTP concurrency.
            if launched_token != self._leader_token:
                del self._inflight[key]
                continue
            try:
                error = future.result()
            except Exception as exc:
                error = exc
            outcome = self._acknowledge(
                key, self._refresh_delay(url,params,failed=error is not None), str(error) if error is not None else "",
                token=launched_token, generation=generation,
            )
            # A busy writer must not forget a finished HTTP request before
            # acknowledging its original generation. Retain the future if
            # _acknowledge raises; the next poll retries only the SQL ACK.
            del self._inflight[key]
            if outcome == "lost_leader":
                self._leader_held = False
                return results
            if outcome == "acknowledged":
                results.append((key, error is None))

        if self._history_pool is not None:
            from .hub_rest_lanes import admit_rest_lanes
            admit_rest_lanes(self)
            return results

        free = self.max_workers - len(self._inflight)
        if free <= 0:
            return results
        # Fetch beyond the free count so a busy regular lane does not hide
        # urgent rows. But never reserve more than one background lane while
        # there is active priority work.
        # Keep actual HTTP execution capacity for BOTH exposed-position
        # protection and historical candle discovery. With six workers a
        # permanently busy urgent queue previously confined 824-symbol
        # history warmup to ONE background lane. The two-lane minimum is
        # conditional on at least three workers; never exhaust every slot
        # with regular HTTP when a new position mark can arrive.
        regular_reserve = min(2, max(1, self.max_workers // 3))
        available = self.registry.due(
            limit=self.max_workers, reserve_regular=regular_reserve,
            exclude=tuple(self._inflight), include_priority=True,
            include_generation=True,
            skip_ws_routes=self._websocket_queue_exclusions(),
        )
        regular_inflight = sum(not item[2] for item in self._inflight.values())
        urgent_inflight = len(self._inflight) - regular_inflight
        urgent_available = any(item[3] for item in available)
        regular_available = any(not item[3] for item in available)
        # Three independent obligations must coexist under mixed load:
        # (1) financial mark/risk work, (2) two background candle lanes on
        # a six-worker Hub, (3) a free slot for the NEXT financial request.
        # The original cap of four urgent + two regular filled all six;
        # adding a physical reserve later left only ONE regular lane.
        # Hold urgent admissions at 6-2-1=3 while regular work is waiting.
        # With a two-worker pool no spare slot is mathematically possible:
        # share one financial + one regular instead.
        urgent_mix_limit = max(
            1, self.max_workers - regular_reserve
            - int(self.max_workers > 2)
        )
        for key, url, params, is_urgent, generation in available:
            if free <= 0:
                break
            if (is_urgent and regular_available
                    and regular_inflight < regular_reserve
                    and urgent_inflight >= urgent_mix_limit):
                continue
            if (not is_urgent and regular_inflight >= regular_reserve
                    and urgent_available):
                continue
            # The queue may mark held-position candle history urgent, but
            # that is NOT financial mark/tier authority. Slow candle HTTP
            # must not fill the last producer slot before an independent
            # risk-tier or executable-market request arrives. Preserve one
            # physical slot for those requests when a parallel pool exists.
            # This does not stop single-worker Hub progress.
            financial_urgent = (is_urgent and
                url != 'https://api.bybit.com/v5/market/kline')
            if self.max_workers > 2 and free == 1 and not financial_urgent:
                continue
            if not is_urgent and regular_inflight >= self.max_workers - 1:
                continue
            def fetch(request_url=url, request_params=params, urgent=is_urgent):
                try:
                    self._fetch_public(request_url, request_params, urgent=urgent)
                    return None
                except Exception as exc:
                    return exc
            self._inflight[key] = (self._pool.submit(fetch), url, is_urgent,
                                   self._leader_token, generation, params)
            urgent_inflight += bool(is_urgent)
            regular_inflight += not is_urgent
            free -= 1
        return results

    def close(self):
        if self._history_pool is not None:
            self._history_pool.shutdown(wait=True, cancel_futures=True)
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=True)
        if self._leader_token:
            self.registry.release_leader(self.owner, token=self._leader_token)
            self._leader_token = None
            self._leader_held = False
        self.source.close()
