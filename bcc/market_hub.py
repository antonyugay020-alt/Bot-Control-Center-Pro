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
            db.execute('''CREATE TABLE IF NOT EXISTS hub_subscriptions(
              k TEXT PRIMARY KEY, url TEXT NOT NULL, params TEXT NOT NULL,
              active_until REAL NOT NULL, next_due REAL NOT NULL DEFAULT 0,
              last_error TEXT NOT NULL DEFAULT '',
              priority_until REAL NOT NULL DEFAULT 0)''')
            columns = {row[1] for row in db.execute('PRAGMA table_info(hub_subscriptions)')}
            if 'priority_until' not in columns:
                db.execute('ALTER TABLE hub_subscriptions ADD COLUMN priority_until REAL NOT NULL DEFAULT 0')
            db.execute('CREATE INDEX IF NOT EXISTS hub_due ON hub_subscriptions(next_due,active_until)')
            db.execute("CREATE TABLE IF NOT EXISTS hub_leader(singleton INTEGER PRIMARY KEY CHECK(singleton=1), owner TEXT NOT NULL, expires REAL NOT NULL, lease_token TEXT NOT NULL DEFAULT '')")
            # Existing installations may have a live legacy leader row. The
            # next admission mints a new token; no old producer can prove it.
            leader_columns = {row[1] for row in db.execute('PRAGMA table_info(hub_leader)')}
            if 'lease_token' not in leader_columns:
                db.execute("ALTER TABLE hub_leader ADD COLUMN lease_token TEXT NOT NULL DEFAULT ''")

    def claim_leader(self, owner, *, lifetime=15):
        if not owner or not 1 <= lifetime <= 120:
            raise ValueError('Invalid hub lease')
        now = time.time()
        candidate = uuid.uuid4().hex
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("""INSERT INTO hub_leader(singleton,owner,expires,lease_token)
              VALUES(1,?,?,?)
              ON CONFLICT(singleton) DO UPDATE SET
                owner=excluded.owner,expires=excluded.expires,
                lease_token=CASE WHEN hub_leader.owner=excluded.owner
                    AND hub_leader.expires>=? AND hub_leader.lease_token<>''
                    THEN hub_leader.lease_token ELSE excluded.lease_token END
              WHERE hub_leader.expires < ? OR hub_leader.owner = ?""",
              (owner, now + lifetime, candidate, now, now, owner))
            claimed = db.execute(
                'SELECT owner,expires,lease_token FROM hub_leader WHERE singleton=1'
            ).fetchone()
            db.commit()
        return bool(claimed and claimed[0] == owner
                    and claimed[1] >= now and claimed[2])

    def leader_token(self, owner):
        """Opaque generation of the *current* unexpired lease, not just PID.

        Releasing and reclaiming even with the same owner invalidates all
        previous HTTP completions. Tokens are never exposed to subscribers.
        """
        with self._connect() as db:
            row = db.execute(
                'SELECT lease_token FROM hub_leader WHERE singleton=1 '
                'AND owner=? AND expires>=? AND lease_token<>\'\'',
                (owner, time.time()),
            ).fetchone()
        return row[0] if row else None

    def renew_leader(self, owner, *, token, lifetime=120):
        # Renewal and release are mutations of the same generation as the
        # subscription acknowledgement. Owner names alone are not fences:
        # an old heartbeat can otherwise extend a reclaimed same-owner lease.
        if (not owner or not isinstance(token, str) or not token
                or not 1 <= lifetime <= 120):
            raise ValueError('Invalid hub lease')
        now = time.time()
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE hub_leader SET expires=? WHERE singleton=1 "
                "AND owner=? AND expires>=? AND lease_token=?",
                (now + lifetime, owner, now, token),
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

    def finish_owned(self, key, delay, owner, error='', *, token):
        """Acknowledge only while this producer still holds an unexpired lease.

        The ownership check and subscription update share one SQLite writer
        transaction so a replacement producer cannot be raced by an old one.
        """
        if not isinstance(token, str) or not token:
            raise ValueError('Hub completion requires a lease fencing token')
        now = time.time()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            valid = db.execute(
                'SELECT 1 FROM hub_leader WHERE singleton=1 AND owner=? '
                'AND expires>=? AND lease_token=?',
                (owner, now, token),
            ).fetchone()
            if not valid:
                db.rollback()
                return False
            db.execute('UPDATE hub_subscriptions SET next_due=?,last_error=? WHERE k=?',
                       (now + delay, str(error)[:300], key))
            db.commit()
        return True

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
        now = time.time()
        with self._connect() as db:
            db.execute('''INSERT INTO hub_subscriptions(k,url,params,active_until,next_due,priority_until)
              VALUES(?,?,?,?,0,?) ON CONFLICT(k) DO UPDATE SET
              active_until=max(hub_subscriptions.active_until,excluded.active_until),
              priority_until=max(hub_subscriptions.priority_until,excluded.priority_until),
              params=excluded.params,
              url=excluded.url,
              next_due=CASE WHEN hub_subscriptions.params<>excluded.params
                            OR hub_subscriptions.url<>excluded.url
                            THEN 0 ELSE hub_subscriptions.next_due END,
              last_error=CASE WHEN hub_subscriptions.params<>excluded.params
                              OR hub_subscriptions.url<>excluded.url
                              THEN '' ELSE hub_subscriptions.last_error END''',
              (key, url, encoded, now + lifetime, now + min(lifetime * 0.75, 90) if priority else 0))
        return key

    def has_active_subscriptions(self):
        """Read-only liveness probe for the independent producer lifecycle.

        Workers renew subscriptions during active reads. Stopped workers no
        longer renew, so the hub may retire after their leases expire.
        """
        with self._connect() as db:
            return db.execute(
                'SELECT 1 FROM hub_subscriptions WHERE active_until>? LIMIT 1',
                (time.time(),),
            ).fetchone() is not None

    def due(self, limit=32, *, reserve_regular=0, exclude=(), include_priority=False):
        # Urgent position marks first, reserve slots for scans.

        # Urgency belongs to a short renewable lease, not a permanent flag.
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
        now = time.time()
        with self._connect() as db:
            urgent = db.execute('''SELECT k,url,params FROM hub_subscriptions
              WHERE active_until>? AND next_due<=? AND priority_until>?''' + skip_sql +
              ' ORDER BY next_due,k LIMIT ?', (now, now, now, *exclude, limit-reserve_regular)).fetchall()
            regular = db.execute('''SELECT k,url,params FROM hub_subscriptions
              WHERE active_until>? AND next_due<=? AND priority_until<=?''' + skip_sql +
              ' ORDER BY next_due,k LIMIT ?', (now, now, now, *exclude, limit-len(urgent))).fetchall()
            rows = [(row, True) for row in urgent] + [(row, False) for row in regular]
            if len(rows) < limit and len(urgent) == limit-reserve_regular:
                more = db.execute('''SELECT k,url,params FROM hub_subscriptions
                  WHERE active_until>? AND next_due<=? AND priority_until>?''' + skip_sql +
                  ' ORDER BY next_due,k LIMIT ? OFFSET ?',
                  (now,now,now,*exclude,limit-len(rows),len(urgent))).fetchall()
                rows.extend((row, True) for row in more)
        # Older Hub databases can contain subscriptions admitted before the
        # shared public-route policy existed. Revalidate at dequeue as well as
        # at registration; invalid rows must not consume producer HTTP slots.
        from .market import PublicData
        valid = []
        for (key, url, raw_params), is_urgent in rows:
            try:
                params = json.loads(raw_params, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
                PublicData.validated_public_request(url, params)
                if self.identity(url, params)[0] != key:
                    raise ValueError('Subscription identity differs from persisted data')
            except (ValueError, TypeError, UnicodeError):
                with self._connect() as db:
                    db.execute('UPDATE hub_subscriptions SET last_error=?, next_due=? '
                               'WHERE k=? AND params=?',
                               ('Invalid persisted public subscription',
                                time.time() + 60, key, raw_params))
                continue
            valid.append((key, url, params, is_urgent) if include_priority else (key, url, params))
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
        if type(max_workers) is not int or not 1 <= max_workers <= 12:
            raise ValueError('Hub producer concurrency must be 1..12')
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

    def run_once(self, limit=32):
        if self.max_workers > 1:
            return self._run_parallel(limit)
        token = self.acquire_leadership()
        if not token:
            return []
        results = []
        for key, url, params in self.registry.due(limit):
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
                self.source.get(url, params=params)
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
            if error is None:
                if not self.registry.finish_owned(key, delay, self.owner, token=token):
                    break
                results.append((key, True))
            else:
                if not self.registry.finish_owned(key, 2.0, self.owner, str(error), token=token):
                    break
                results.append((key, False))
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
        due = self.registry.due(min(limit, self.max_workers),
                                reserve_regular=1 if self.max_workers > 1 else 0)
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
            key, url, params = request
            try:
                self.source.get(url, params=params)
                return key, url, None
            except Exception as exc:
                return key, url, exc

        keeper = threading.Thread(target=keep_leader, name='hub-lease-renewal', daemon=True)
        keeper.start()
        results = []
        try:
            futures = [self._pool.submit(fetch, request) for request in due]
            for future in as_completed(futures):
                key, url, error = future.result()
                if lost.is_set() or not self.registry.renew_leader(self.owner, token=token, lifetime=120):
                    lost.set()
                    break
                delay = 120.0 if '/risk-limit' in url else (.25 if 'orderbook' in url or '/book' in url else 1.0)
                if error is not None:
                    delay = 2.0
                if not self.registry.finish_owned(key, delay, self.owner,
                                                 str(error) if error is not None else '',
                                                 token=token):
                    lost.set()
                    break
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

        results = []
        for key, (future, url, is_urgent, launched_token) in list(self._inflight.items()):
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
            if not self.registry.finish_owned(key, delay, self.owner,
                                             str(error) if error is not None else '',
                                             token=launched_token):
                self._leader_held = False
                return results
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
        )
        regular_inflight = sum(not item[2] for item in self._inflight.values())
        urgent_inflight = len(self._inflight) - regular_inflight
        urgent_available = any(item[3] for item in available)
        regular_available = any(not item[3] for item in available)
        for key, url, params, is_urgent in available:
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
                # The producer, not the consumer, reserves the exchange GET
                # budget. Carry the trusted subscription urgency across this
                # boundary, on the producer's own thread-local context only.
                context = getattr(self.source, '_thread', None)
                previous = getattr(context, 'position_mark', False) if context is not None else False
                if context is not None:
                    context.position_mark = bool(urgent)
                try:
                    self.source.get(request_url, params=request_params)
                    return None
                except Exception as exc:
                    return exc
                finally:
                    if context is not None:
                        context.position_mark = previous
            self._inflight[key] = (self._pool.submit(fetch), url, is_urgent, self._leader_token)
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
