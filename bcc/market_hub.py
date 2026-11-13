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

    def subscribe(self, url, params, *, lifetime=60, priority=False):
        if (type(lifetime) not in (int, float) or not math.isfinite(lifetime)
                or not 0 < lifetime <= 3600):
            raise ValueError('Hub subscription lifetime must be finite and positive')
        if type(priority) is not bool:
            raise ValueError('Hub priority flag must be boolean')
        from .market import PublicData
        PublicData.validated_public_request(url, params)
        key, encoded = self.identity(url, params)
        generation = uuid.uuid4().hex
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            # A subscription may expire while waiting on a fleet-wide writer.
            # Refresh its incarnation and TTL from transaction admission.
            now = time.time()
            db.execute('''INSERT INTO hub_subscriptions(k,url,params,active_until,next_due,priority_until,subscription_token)
              VALUES(?,?,?,?,0,?,?) ON CONFLICT(k) DO UPDATE SET
              active_until=max(hub_subscriptions.active_until,excluded.active_until),
              subscription_token=CASE WHEN hub_subscriptions.active_until<=?
                             OR hub_subscriptions.params<>excluded.params
                             OR hub_subscriptions.url<>excluded.url
                             THEN excluded.subscription_token
                             ELSE hub_subscriptions.subscription_token END,
              priority_until=CASE WHEN hub_subscriptions.active_until<=?
                             THEN excluded.priority_until
                             ELSE max(hub_subscriptions.priority_until,excluded.priority_until) END,
              params=excluded.params,
              url=excluded.url,
              next_due=CASE WHEN hub_subscriptions.active_until<=?
                            OR hub_subscriptions.params<>excluded.params
                            OR hub_subscriptions.url<>excluded.url
                            THEN 0 ELSE hub_subscriptions.next_due END,
              last_error=CASE WHEN hub_subscriptions.active_until<=?
                              OR hub_subscriptions.params<>excluded.params
                              OR hub_subscriptions.url<>excluded.url
                              THEN '' ELSE hub_subscriptions.last_error END''',
              (key, url, encoded, now + lifetime,
               now + min(lifetime * 0.75, 90) if priority else 0,
               generation, now, now, now, now))
        return key

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
            include_generation=False, skip_ws_routes=()):
        # Urgent position marks first, reserve slots for scans.

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
                if route.endswith("/tickers"):
                    shape = ("coalesce(json_type(params,'$.symbol')='text',0) "
                             "AND json_extract(params,'$.category')='linear'")
                else:
                    shape = ("coalesce(json_type(params,'$.symbol')='text',0) "
                             "AND json_extract(params,'$.category')='linear' "
                             "AND json_extract(params,'$.limit')=50")
                clauses.append("NOT (url=? AND CASE WHEN json_valid(params) "
                               "THEN (" + shape + ") ELSE 0 END)")
            skip_sql += ' AND ' + ' AND '.join(clauses)
        with self._connect() as db:
            now = time.time()
            urgent = db.execute('''SELECT k,url,params,subscription_token FROM hub_subscriptions
              WHERE active_until>? AND next_due<=? AND priority_until>?''' + skip_sql +
              ' ORDER BY next_due,k LIMIT ?', (now, now, now, *exclude, *skip_ws_routes, limit-reserve_regular)).fetchall()
            regular = db.execute('''SELECT k,url,params,subscription_token FROM hub_subscriptions
              WHERE active_until>? AND next_due<=? AND priority_until<=?''' + skip_sql +
              ' ORDER BY next_due,k LIMIT ?', (now, now, now, *exclude, *skip_ws_routes, limit-len(urgent))).fetchall()
            rows = [(row, True) for row in urgent] + [(row, False) for row in regular]
            if len(rows) < limit and len(urgent) == limit-reserve_regular:
                more = db.execute('''SELECT k,url,params,subscription_token FROM hub_subscriptions
                  WHERE active_until>? AND next_due<=? AND priority_until>?''' + skip_sql +
                  ' ORDER BY next_due,k LIMIT ? OFFSET ?',
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
        self.lease_renew_interval = 5.0
        self.source = source or PublicData()
        if self.source.shared is None:
            self.source.shared = PublicCache(cache_path)

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

    def publish_ws_event(self, kind, symbol):
        """Publish one verified stream event without a per-symbol GET poll.

        An independent SQL transaction checks leader, active subscription,
        provider fence and cache publication as one atomic decision.
        """
        from .ws_quotes import StreamUnavailable
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
        else:
            raise ValueError("unknown WS publication channel")
        if stream is None:
            return False
        try:
            if kind == "tickers":
                bid, ask, at = stream.quote(symbol)
                if (not all(math.isfinite(x) for x in (bid, ask, at))
                        or bid <= 0 or ask <= 0 or bid > ask
                        or not -5 <= time.time() - at <= 2):
                    return False
                value = {
                    "retCode": 0, "retMsg": "OK", "_bcc_ws_quote": True,
                    "time": int(at * 1000),
                    "result": {"list": [{
                        "symbol": symbol,
                        "bid1Price": str(bid), "ask1Price": str(ask),
                    }]},
                }
                ttl = .35
            else:
                book = stream.get_orderbook(symbol)
                stamp = book["ts"]
                if (not math.isfinite(stamp) or
                        not -.5 <= time.time() - stamp <= 3):
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
                ttl = .2
        except (StreamUnavailable, ValueError, KeyError, TypeError):
            return False
        from .data_cache import CacheBusy
        try:
            return self.source.shared.publish_websocket(
                url, params, "api.bybit.com", value, ttl=ttl,
                leader_owner=self.owner, leader_token=self._leader_token,
            )
        except CacheBusy:
            # Queue worker may retry a newer event on the next receipt.
            return False

    def _websocket_queue_exclusions(self):
        routes = []
        if getattr(self.source, "_quote_stream", None) is not None:
            routes.append("https://api.bybit.com/v5/market/tickers")
        if getattr(self.source, "_orderbook_stream", None) is not None:
            routes.append("https://api.bybit.com/v5/market/orderbook")
        return tuple(routes)

    def subscribe_active_websockets(self):
        """Pre-register every demanded symbol on the shared WS producers."""
        if self._leader_token is None:
            return
        now = time.monotonic()
        if now < self._ws_subscription_scan_due:
            return
        self._ws_subscription_scan_due = now + 1.0
        ticker = getattr(self.source, "_quote_stream", None)
        orderbook = getattr(self.source, "_orderbook_stream", None)
        if ticker is None and orderbook is None:
            return
        tickers, books = self.registry.active_bybit_symbols()
        if ticker is not None:
            for symbol in sorted(tickers):
                try:
                    ticker.subscribe(symbol)
                except ValueError:
                    # One malformed or incompatible symbol cannot terminate
                    # the shared producer for unrelated valid bot streams.
                    continue
        if orderbook is not None:
            for symbol in sorted(books):
                try:
                    orderbook.subscribe(symbol)
                except ValueError:
                    continue

    def _fetch_public(self, url, params, *, urgent):
        """Preserve subscriber priority across ALL Hub producer modes.

        PublicData checks its thread-local position_mark before reserving the
        shared Bybit request budget. A priority queue alone cannot protect
        open PAPER positions if the producer drops that mark at the HTTP
        boundary. Always restore the worker-thread context, including errors.
        """
        context = getattr(self.source, '_thread', None)
        previous = getattr(context, 'position_mark', False) if context is not None else False
        if context is not None:
            context.position_mark = bool(urgent)
        try:
            return self.source.get(url, params=params)
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
            delay = 120.0 if '/risk-limit' in url else (.25 if 'orderbook' in url or '/book' in url else 1.0)
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
                key, 2.0 if error is not None else delay,
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
                return key, url, generation, None
            except Exception as exc:
                return key, url, generation, exc

        keeper = threading.Thread(target=keep_leader, name='hub-lease-renewal', daemon=True)
        keeper.start()
        results = []
        try:
            futures = [self._pool.submit(fetch, request) for request in due]
            for future in as_completed(futures):
                key, url, generation, error = future.result()
                if lost.is_set() or not self.registry.renew_leader(self.owner, token=token, lifetime=120):
                    lost.set()
                    break
                delay = 120.0 if '/risk-limit' in url else (.25 if 'orderbook' in url or '/book' in url else 1.0)
                if error is not None:
                    delay = 2.0
                outcome = self._acknowledge(
                    key, delay, str(error) if error is not None else "",
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

    def poll_nonblocking(self):
        """Run one short producer scheduling turn without awaiting slow HTTP.

        A slow regular request must not delay urgent marks on free workers.
        Leader ownership is renewed on a bounded cadence, and every completed
        acknowledgement is fenced by the authoritative SQLite leader lease.
        Only the Hub service uses this method; run_once stays synchronous for
        deterministic offline tests and existing integrations.
        """
        if self._pool is None:
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

        self._maintain_registry()
        self.subscribe_active_websockets()
        results = []
        for key, (future, url, is_urgent, launched_token, generation) in list(self._inflight.items()):
            if not future.done():
                continue
            del self._inflight[key]
            # A task launched under an earlier lease must not acknowledge
            # subscriptions after the producer loses and regains leadership.
            # Keep it accounted for until completion to bound HTTP concurrency.
            if launched_token != self._leader_token:
                continue
            try:
                error = future.result()
            except Exception as exc:
                error = exc
            delay = (2.0 if error is not None else
                     120.0 if '/risk-limit' in url else (.25 if 'orderbook' in url or '/book' in url else 1.0))
            outcome = self._acknowledge(
                key, delay, str(error) if error is not None else "",
                token=launched_token, generation=generation,
            )
            if outcome == "lost_leader":
                self._leader_held = False
                return results
            if outcome == "acknowledged":
                results.append((key, error is None))

        free = self.max_workers - len(self._inflight)
        if free <= 0:
            return results
        # Fetch beyond the free count so a busy regular lane does not hide
        # urgent rows. But never reserve more than one background lane while
        # there is active priority work.
        available = self.registry.due(
            limit=self.max_workers, reserve_regular=1,
            exclude=tuple(self._inflight), include_priority=True,
            include_generation=True,
            skip_ws_routes=self._websocket_queue_exclusions(),
        )
        regular_inflight = sum(not item[2] for item in self._inflight.values())
        urgent_inflight = len(self._inflight) - regular_inflight
        urgent_available = any(item[3] for item in available)
        regular_available = any(not item[3] for item in available)
        for key, url, params, is_urgent, generation in available:
            if free <= 0:
                break
            if (is_urgent and regular_available and regular_inflight == 0
                    and urgent_inflight >= self.max_workers - 1):
                continue
            if not is_urgent and regular_inflight > 0 and urgent_available:
                continue
            # Keep one actual executor slot idle for a position that becomes
            # urgent *after* slow scanner HTTP calls are already in flight.
            # Merely reserving an urgent SQL queue slot is insufficient.
            if not is_urgent and regular_inflight >= self.max_workers - 1:
                continue
            def fetch(request_url=url, request_params=params, urgent=is_urgent):
                try:
                    self._fetch_public(request_url, request_params, urgent=urgent)
                    return None
                except Exception as exc:
                    return exc
            self._inflight[key] = (self._pool.submit(fetch), url, is_urgent,
                                   self._leader_token, generation)
            urgent_inflight += bool(is_urgent)
            regular_inflight += not is_urgent
            free -= 1
        return results

    def close(self):
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=True)
        if self._leader_token:
            self.registry.release_leader(self.owner, token=self._leader_token)
            self._leader_token = None
            self._leader_held = False
        self.source.close()
