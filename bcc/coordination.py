"""Durable, process-independent ownership control for registered bot instances.

Each renewal and transition is a SQLite transaction. A monotonically increasing
fencing generation prevents an old worker from mutating authority after a lease
expires and another process claims it. Callers must validate their fence in the
same database transaction as any protected write; a preflight check alone is
not a substitute for transactional fencing.
"""
import sqlite3
import math
import time
from contextlib import contextmanager
from pathlib import Path

from .trade_fence import command_gate


class StaleOwner(RuntimeError):
    """Worker does not hold the current generation of ownership."""


class BusyOwner(RuntimeError):
    """Another worker holds an unexpired claim."""


class Coordinator:
    def __init__(self, path, *, clock=None):
        self.path = Path(path)
        self.clock = clock or time.time

    @contextmanager
    def connection(self):
        # Never open an uninitialized DB for reading. Bootstrap is explicit.
        db = sqlite3.connect(self.path.resolve().as_uri() + '?mode=rw', timeout=15, isolation_level=None, uri=True)
        try:
            # Connection context commits/rolls back but does not close.
            # Command/ledger fences must release OS handles deterministically.
            with db:
                db.execute('PRAGMA busy_timeout=15000')
                yield db
        finally:
            db.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                db.execute('''CREATE TABLE IF NOT EXISTS owners(
                    key TEXT PRIMARY KEY, generation INTEGER NOT NULL DEFAULT 0 CHECK(generation>=0),
                    claimant TEXT, expires REAL NOT NULL DEFAULT 0,
                    desired TEXT NOT NULL DEFAULT 'STOPPED' CHECK(desired IN ('RUNNING','STOPPED')),
                    config_version INTEGER NOT NULL DEFAULT 1 CHECK(config_version>0),
                    force_stop INTEGER NOT NULL DEFAULT 0 CHECK(force_stop IN (0,1)),
                    command_version INTEGER NOT NULL DEFAULT 0 CHECK(command_version>=0),
                    retiring INTEGER NOT NULL DEFAULT 0 CHECK(retiring IN (0,1))
                )''')
                # Forward migration for existing installations without a force-stop column.
                columns = {row[1] for row in db.execute('PRAGMA table_info(owners)')}
                if 'force_stop' not in columns:
                    db.execute('ALTER TABLE owners ADD COLUMN force_stop INTEGER NOT NULL DEFAULT 0')
                if 'command_version' not in columns:
                    db.execute('ALTER TABLE owners ADD COLUMN command_version INTEGER NOT NULL DEFAULT 0')
                if 'retiring' not in columns:
                    db.execute('ALTER TABLE owners ADD COLUMN retiring INTEGER NOT NULL DEFAULT 0 CHECK(retiring IN (0,1))')
                db.execute("""CREATE TABLE IF NOT EXISTS drain_commands(
                    key TEXT PRIMARY KEY REFERENCES owners(key),
                    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0),
                    applied_revision INTEGER NOT NULL DEFAULT 0 CHECK(applied_revision>=0),
                    draining INTEGER NOT NULL CHECK(draining IN (0,1)),
                    launch_revision INTEGER NOT NULL DEFAULT 0 CHECK(launch_revision>=0)
                )""")
                drain_columns = {row[1] for row in db.execute('PRAGMA table_info(drain_commands)')}
                if 'launch_revision' not in drain_columns:
                    db.execute('ALTER TABLE drain_commands ADD COLUMN launch_revision INTEGER NOT NULL DEFAULT 0')
                db.execute("""CREATE TABLE IF NOT EXISTS manual_close_commands(
                    key TEXT PRIMARY KEY REFERENCES owners(key),
                    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0),
                    applied_revision INTEGER NOT NULL DEFAULT 0 CHECK(applied_revision>=0),
                    launch_revision INTEGER NOT NULL DEFAULT 0 CHECK(launch_revision>=0)
                )""")
                close_columns = {row[1] for row in db.execute('PRAGMA table_info(manual_close_commands)')}
                if 'launch_revision' not in close_columns:
                    db.execute('ALTER TABLE manual_close_commands ADD COLUMN launch_revision INTEGER NOT NULL DEFAULT 0')
                db.execute("""CREATE TABLE IF NOT EXISTS command_observations(
                    key TEXT PRIMARY KEY REFERENCES owners(key),
                    revision INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('queued','running','applied','error')),
                    observed TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                )""")
                # Durable tombstones are distinct from active registrations.
                # A GUI that resolved a path just before another GUI deletes
                # it must not resurrect that key with a late Start command.
                db.execute('''CREATE TABLE IF NOT EXISTS retired_owners(
                    key TEXT PRIMARY KEY, retired_at REAL NOT NULL
                )''')
                db.execute('CREATE INDEX IF NOT EXISTS owners_force_stop_idx ON owners(force_stop, key)')
                db.execute("""CREATE TABLE IF NOT EXISTS restart_policy(
                    key TEXT PRIMARY KEY REFERENCES owners(key),
                    failures INTEGER NOT NULL DEFAULT 0 CHECK(failures>=0),
                    retry_after REAL NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    spawn_epoch INTEGER NOT NULL DEFAULT 0 CHECK(spawn_epoch>=0),
                    handled_exit INTEGER NOT NULL DEFAULT 0 CHECK(handled_exit>=0),
                    spawned_at REAL NOT NULL DEFAULT 0
                )""")
                # Existing users keep their retry history across upgrades.
                known = {row[1] for row in db.execute('PRAGMA table_info(restart_policy)')}
                for name, definition in (
                    ('spawn_epoch', 'INTEGER NOT NULL DEFAULT 0'),
                    ('handled_exit', 'INTEGER NOT NULL DEFAULT 0'),
                    ('spawned_at', 'REAL NOT NULL DEFAULT 0'),
                ):
                    if name not in known:
                        db.execute(f'ALTER TABLE restart_policy ADD COLUMN {name} {definition}')
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def _control_gate(self, key):
        return command_gate(self.path.parent, key)

    @contextmanager
    def _write(self):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _name(value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError('nonempty identity required')
        return value

    def register(self, key):
        self._name(key)
        with self._write() as db:
            self._assert_not_retired(db, key)
            self._assert_not_retiring(db, key)
            db.execute('INSERT OR IGNORE INTO owners(key) VALUES(?)', (key,))

    def register_discovered(self, key):
        """Idempotent inventory admission; retirement is not an inventory fault.

        A filesystem entry may remain after a crash during deletion. Strict
        operator registration still rejects that identity; discovery merely
        skips it and must continue to the rest of the fleet.
        """
        self._name(key)
        with self._write() as db:
            if db.execute('SELECT 1 FROM retired_owners WHERE key=?',
                          (key,)).fetchone():
                return False
            row = db.execute('SELECT retiring FROM owners WHERE key=?',
                             (key,)).fetchone()
            if row is not None:
                return not bool(row[0])
            db.execute('INSERT INTO owners(key) VALUES(?)', (key,))
            return True

    def begin_retirement(self, key):
        '''Fence all GUI processes before physical deletion of a bot.

        Commands published by another window cannot revive or reuse an owner
        between the STOP intent and deletion/unregistration. A failed disk
        delete keeps this durable, fail-closed marker for an explicit retry.
        '''
        self._name(key)
        with self._control_gate(key), self._write() as db:
            # Deletion may race discovery: the filesystem bot can exist before
            # inventory has registered it. Establish the same durable fence
            # even for an unknown owner, otherwise a delayed GUI START could
            # recreate the deleted identity after the directory is removed.
            self._assert_not_retired(db, key)
            db.execute('INSERT OR IGNORE INTO owners(key) VALUES(?)', (key,))
            row = db.execute('SELECT command_version FROM owners WHERE key=?', (key,)).fetchone()
            revision = row[0] + 1
            db.execute('''UPDATE owners SET retiring=1, desired='STOPPED',
                force_stop=0, command_version=? WHERE key=?''', (revision, key))
            db.execute('''INSERT INTO command_observations(key,revision,state,updated_at)
                VALUES(?,?,'queued',?) ON CONFLICT(key) DO UPDATE SET
                revision=excluded.revision,state='queued',observed='',error='',
                updated_at=excluded.updated_at''', (key, revision, float(self.clock())))
            return True

    @staticmethod
    def _assert_not_retired(db, key):
        if db.execute('SELECT 1 FROM retired_owners WHERE key=?', (key,)).fetchone():
            raise ValueError('Бот уже удалён; повторная регистрация запрещена')

    @staticmethod
    def _assert_not_retiring(db, key):
        row = db.execute('SELECT retiring FROM owners WHERE key=?', (key,)).fetchone()
        if row is not None and row[0]:
            raise ValueError('Бот удаляется; новые команды запрещены')

    def unregister(self, key):
        """Forget a physically deleted bot only after its launch was revoked.

        This runs in one transaction: no new instance may inherit the old
        command generation, retry state, or pending financial commands.
        """
        self._name(key)
        with self._control_gate(key), self._write() as db:
            row = db.execute('SELECT desired FROM owners WHERE key=?', (key,)).fetchone()
            if row is None:
                return False
            if row[0] != 'STOPPED':
                raise ValueError('Сначала остановите исполнение бота')
            # Tombstone and retirement share the same writer transaction;
            # no GUI may insert a new RUNNING owner after this commit.
            db.execute('INSERT OR IGNORE INTO retired_owners(key, retired_at) VALUES(?,?)',
                       (key, float(self.clock())))
            for table in ('manual_close_commands', 'drain_commands',
                          'command_observations', 'restart_policy'):
                db.execute(f'DELETE FROM {table} WHERE key=?', (key,))
            return bool(db.execute('DELETE FROM owners WHERE key=? AND desired=?',
                                   (key, 'STOPPED')).rowcount)

    def quiescent_operator_change(self, check_stopped, publish):
        """Publish host policy only against an unchanged, stopped registry.

        Slow filesystem/OS inspection runs without a SQLite writer lock, so
        STOP stays responsive. Every START/STOP increments command_version;
        compare the complete snapshot under the same writer admission used
        by commands before the short atomic policy-file replacement. Even a
        START -> STOP round trip during inspection invalidates the snapshot.
        No financial ledger or control revision is modified by this barrier.
        """
        with self.connection() as db:
            def registry():
                return db.execute('SELECT key,desired,command_version FROM owners ORDER BY key').fetchall()
            before = registry()
            if any(row[1] == 'RUNNING' for row in before):
                raise BusyOwner('Остановите ботов: есть принятая команда запуска')
            check_stopped()
            db.execute('BEGIN IMMEDIATE')
            try:
                if registry() != before:
                    raise BusyOwner('Управление ботами изменилось во время проверки; повторите сохранение лимитов')
                return publish()
            finally:
                # This transaction is an admission barrier, not a DB mutation.
                db.rollback()

    def submit_desired(self, key, state, *, force=False, discard_previous_exit=False):
        """Atomically register, revise intent, publish status and clear retry delay.

        Unlike a sequence of independent writes, a crash cannot leave an
        unobserved command revision or partially-cleared restart policy.
        OS process inspection and execution are deliberately outside this
        transaction; only the runtime service performs those operations.
        """
        self._name(key)
        if state not in ('RUNNING', 'STOPPED'):
            raise ValueError('invalid desired state')
        with self._control_gate(key), self._write() as db:
            self._assert_not_retired(db, key)
            self._assert_not_retiring(db, key)
            db.execute('INSERT OR IGNORE INTO owners(key) VALUES(?)', (key,))
            db.execute("""UPDATE owners SET desired=?, force_stop=?,
                command_version=command_version+1 WHERE key=?""",
                (state, int(bool(force and state == 'STOPPED')), key))
            revision = db.execute('SELECT command_version FROM owners WHERE key=?',
                                  (key,)).fetchone()[0]
            db.execute("""INSERT INTO command_observations(key,revision,state,updated_at)
                VALUES(?,?,'queued',?) ON CONFLICT(key) DO UPDATE SET
                revision=excluded.revision,state='queued',observed='',error='',
                updated_at=excluded.updated_at""",
                (key, revision, float(self.clock())))
            db.execute("""UPDATE restart_policy SET failures=0,retry_after=0,last_error='',
                handled_exit=CASE WHEN ? THEN spawn_epoch ELSE handled_exit END
                WHERE key=?""", (int(bool(discard_previous_exit)), key))
            return revision

    def set_desired(self, key, state, *, force=False):
        self._name(key)
        if state not in ('RUNNING', 'STOPPED'):
            raise ValueError('invalid desired state')
        with self._control_gate(key), self._write() as db:
            self._assert_not_retiring(db, key)
            cur = db.execute('UPDATE owners SET desired=?, force_stop=?, command_version=command_version+1 WHERE key=?', (state, int(bool(force and state == 'STOPPED')), key))
            if not cur.rowcount:
                raise KeyError(key)
            revision = db.execute('SELECT command_version FROM owners WHERE key=?', (key,)).fetchone()[0]
            db.execute('''INSERT INTO command_observations(key,revision,state,updated_at)
                VALUES(?,?,'queued',?) ON CONFLICT(key) DO UPDATE SET
                revision=excluded.revision,state='queued',observed='',error='',updated_at=excluded.updated_at''',
                (key, revision, float(self.clock())))

    def observe_command(self, key, revision, *, state, observed='', error=''):
        """CAS transition: obsolete workers cannot acknowledge newer intents.

        The observed OS state is evidence, not an intent to launch. A start
        is complete only after the worker is observed alive; a stop only when
        it is observed exited. Errors are observable without losing the intent.
        """
        if state not in ('running', 'applied', 'error'):
            raise ValueError('invalid observation state')
        if type(revision) is not int or revision < 0:
            raise ValueError('invalid revision')
        with self._write() as db:
            return bool(db.execute('''UPDATE command_observations
                SET state=?,observed=?,error=?,updated_at=?
                WHERE key=? AND revision=? AND EXISTS(
                    SELECT 1 FROM owners WHERE owners.key=? AND owners.command_version=?)
                AND (state!='applied' OR ?='applied')''',
                (state, str(observed), str(error)[:2048], float(self.clock()),
                 key, revision, key, revision, state)).rowcount)

    def command_status(self, key):
        self._name(key)
        with self.connection() as db:
            row = db.execute('''SELECT o.command_version,o.desired,
                c.state,c.observed,c.error,c.updated_at FROM owners o
                LEFT JOIN command_observations c ON c.key=o.key AND c.revision=o.command_version
                WHERE o.key=?''', (key,)).fetchone()
            if row is None:
                raise KeyError(key)
            return dict(revision=row[0], desired=row[1],
                        state=row[2] or 'unknown', observed=row[3] or '',
                        error=row[4] or '', updated_at=row[5])

    def submit_drain_intent(self, key, draining):
        """Accept drain/resume from any GUI in one durable transaction.

        Discovery is idempotent but never happens in a separate transaction
        from the command publication: process crashes cannot lose the intent.
        """
        self._name(key)
        with self._write() as db:
            self._assert_not_retired(db, key)
            self._assert_not_retiring(db, key)
            db.execute('INSERT OR IGNORE INTO owners(key) VALUES(?)', (key,))
            launch_revision = db.execute(
                'SELECT command_version FROM owners WHERE key=?', (key,)).fetchone()[0]
            db.execute("""INSERT INTO drain_commands(key,revision,draining,launch_revision)
                VALUES(?,1,?,?) ON CONFLICT(key) DO UPDATE SET revision=revision+1,
                draining=excluded.draining,launch_revision=excluded.launch_revision""",
                (key, int(bool(draining)), launch_revision))
            return db.execute('SELECT revision FROM drain_commands WHERE key=?',
                              (key,)).fetchone()[0]

    def submit_manual_close_intent(self, key):
        """Atomically validate the currently authorized PAPER incarnation and
        publish its manual-close command, without process inspection.
        """
        self._name(key)
        with self._write() as db:
            # The bot may have been created by another window or inventoried
            # already. Registration and admission still form one transaction.
            self._assert_not_retired(db, key)
            self._assert_not_retiring(db, key)
            db.execute('INSERT OR IGNORE INTO owners(key) VALUES(?)', (key,))
            row = db.execute('SELECT desired,command_version FROM owners WHERE key=?',
                             (key,)).fetchone()
            if row[0] != 'RUNNING':
                raise ValueError('Нельзя закрыть позиции остановленного PAPER-бота')
            db.execute("""INSERT INTO manual_close_commands(key,revision,launch_revision)
                VALUES(?,1,?) ON CONFLICT(key) DO UPDATE SET
                revision=revision+1,launch_revision=excluded.launch_revision""",
                (key, row[1]))
            return db.execute('SELECT revision FROM manual_close_commands WHERE key=?',
                              (key,)).fetchone()[0]

    def set_drain(self, key, draining):
        """Durable last-intent-wins command, never executed by the UI process."""
        self._name(key)
        with self._write() as db:
            self._assert_not_retiring(db, key)
            if db.execute('SELECT 1 FROM owners WHERE key=?', (key,)).fetchone() is None:
                raise KeyError(key)
            launch_revision = db.execute(
                'SELECT command_version FROM owners WHERE key=?', (key,)).fetchone()[0]
            db.execute("""INSERT INTO drain_commands(key,revision,draining,launch_revision)
                VALUES(?,1,?,?) ON CONFLICT(key) DO UPDATE SET revision=revision+1,
                draining=excluded.draining,launch_revision=excluded.launch_revision""",
                (key, int(bool(draining)), launch_revision))

    def pending_drain(self, key, *, launch_revision=None):
        """Read a pending drain for this exact authorized worker incarnation.

        Older compatibility callers may omit launch_revision. Runtime execution
        must always supply it, so a Stop/Start cannot replay an older command.
        """
        with self.connection() as db:
            if launch_revision is None:
                return db.execute("""SELECT revision,draining FROM drain_commands
                    WHERE key=? AND revision>applied_revision""", (key,)).fetchone()
            if type(launch_revision) is not int or launch_revision < 0:
                return None
            return db.execute("""SELECT d.revision,d.draining FROM drain_commands d
                JOIN owners o ON o.key=d.key WHERE d.key=?
                AND d.revision>d.applied_revision
                AND d.launch_revision=? AND o.command_version=?
                AND o.desired='RUNNING'""",
                (key, launch_revision, launch_revision)).fetchone()

    def acknowledge_drain(self, key, revision, *, launch_revision=None):
        with self._write() as db:
            if launch_revision is None:
                return bool(db.execute("""UPDATE drain_commands SET applied_revision=?
                    WHERE key=? AND revision=? AND applied_revision<?""",
                    (revision, key, revision, revision)).rowcount)
            if type(launch_revision) is not int or launch_revision < 0:
                return False
            return bool(db.execute("""UPDATE drain_commands SET applied_revision=?
                WHERE key=? AND revision=? AND applied_revision<?
                AND launch_revision=? AND EXISTS(
                    SELECT 1 FROM owners o WHERE o.key=? AND o.desired='RUNNING'
                    AND o.command_version=?)""",
                (revision, key, revision, revision, launch_revision, key, launch_revision)).rowcount)

    def request_manual_close(self, key):
        """Durably queue an idempotent PAPER manual-close request."""
        self._name(key)
        with self._write() as db:
            owner = db.execute('SELECT desired,command_version FROM owners WHERE key=?',
                               (key,)).fetchone()
            if owner is None:
                raise KeyError(key)
            if owner[0] != 'RUNNING':
                raise ValueError('Нельзя закрыть позиции остановленного PAPER-бота')
            db.execute("""INSERT INTO manual_close_commands(key,revision,launch_revision)
                VALUES(?,1,?) ON CONFLICT(key) DO UPDATE SET
                revision=revision+1,launch_revision=excluded.launch_revision""", (key, owner[1]))
            return db.execute('SELECT revision FROM manual_close_commands WHERE key=?',
                              (key,)).fetchone()[0]

    def pending_manual_close(self, key, *, launch_revision=None):
        """Return only a command for the exact, still-authorized worker epoch."""
        self._name(key)
        if type(launch_revision) is not int or launch_revision < 0:
            return None
        with self.connection() as db:
            row = db.execute("""SELECT c.revision FROM manual_close_commands c
                JOIN owners o ON o.key=c.key
                WHERE c.key=? AND c.revision>c.applied_revision
                AND c.launch_revision=? AND o.command_version=?
                AND o.desired='RUNNING'""", (key, launch_revision, launch_revision)).fetchone()
            return row[0] if row else None

    def acknowledge_manual_close(self, key, revision):
        """A stale acknowledgement cannot erase a newer close request."""
        if type(revision) is not int or revision < 1:
            raise ValueError('invalid manual-close revision')
        with self._write() as db:
            return bool(db.execute("""UPDATE manual_close_commands
                SET applied_revision=? WHERE key=? AND revision=?
                AND applied_revision<? AND EXISTS(
                    SELECT 1 FROM owners o WHERE o.key=? AND o.desired='RUNNING'
                    AND o.command_version=manual_close_commands.launch_revision)""",
                (revision, key, revision, revision, key)).rowcount)

    def clear_force_stop(self, key, expected_version):
        """Acknowledge exactly the stop observed, never a newer command."""
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError('invalid command revision')
        with self._write() as db:
            return bool(db.execute(
                'UPDATE owners SET force_stop=0 WHERE key=? AND desired=? AND command_version=?',
                (key, 'STOPPED', expected_version)).rowcount)

    def update_config_version(self, key, expected):
        self._name(key)
        if type(expected) is not int or expected < 1:
            raise ValueError('invalid config version')
        with self._write() as db:
            cur = db.execute('UPDATE owners SET config_version=config_version+1 WHERE key=? AND config_version=?', (key, expected))
            if not cur.rowcount:
                raise StaleOwner('config revision mismatch')
            return expected + 1

    def acquire(self, key, claimant, ttl):
        self._name(key)
        self._name(claimant)
        if type(ttl) not in (int, float) or not math.isfinite(ttl) or not 0 < ttl < 86400:
            raise ValueError('invalid lease duration')
        with self._write() as db:
            row = db.execute('SELECT generation,claimant,expires,desired FROM owners WHERE key=?', (key,)).fetchone()
            if row is None:
                raise KeyError(key)
            generation, current, expires, desired = row
            now = float(self.clock())
            if desired != 'RUNNING':
                raise StaleOwner('not requested to run')
            if current is not None and expires > now:
                raise BusyOwner(key)
            generation += 1
            db.execute('UPDATE owners SET generation=?,claimant=?,expires=? WHERE key=?',
                       (generation, claimant, now + ttl, key))
            return generation

    def assert_owner(self, db, key, claimant, generation):
        """Run inside the SAME SQLite transaction as a protected state mutation."""
        row = db.execute('SELECT generation,claimant,expires,desired FROM owners WHERE key=?', (key,)).fetchone()
        if (not row or type(generation) is not int or row[0] != generation
                or row[1] != claimant or row[3] != 'RUNNING'
                or row[2] <= float(self.clock())):
            raise StaleOwner(key)

    def renew(self, key, claimant, generation, ttl):
        if type(ttl) not in (int, float) or not math.isfinite(ttl) or not 0 < ttl < 86400:
            raise ValueError('invalid lease duration')
        with self._write() as db:
            self.assert_owner(db, key, claimant, generation)
            db.execute('UPDATE owners SET expires=? WHERE key=?', (float(self.clock()) + ttl, key))

    def release(self, key, claimant, generation):
        with self._write() as db:
            self.assert_owner(db, key, claimant, generation)
            db.execute('UPDATE owners SET claimant=NULL,expires=0 WHERE key=?', (key,))

    def has_registrations(self):
        """O(1) presence probe for truthful empty-registry service readiness.

        An empty transient Fleet queue says nothing about durable bots: failed
        reconciliation tasks retire their ephemeral keys too.
        """
        with self.connection() as db:
            return db.execute('SELECT 1 FROM owners LIMIT 1').fetchone() is not None

    def snapshot(self):
        with self.connection() as db:
            return [dict(key=k, generation=g, claimant=c, expires=x,
                         desired=d, config_version=v, force_stop=bool(f)) for k,g,c,x,d,v,f in
                    db.execute('SELECT key,generation,claimant,expires,desired,config_version,force_stop FROM owners ORDER BY key')]

    def get_registration(self, key):
        """Read one bot without scanning the complete durable registry."""
        self._name(key)
        with self.connection() as db:
            row = db.execute("""SELECT o.key,o.desired,o.force_stop,o.command_version,
                COALESCE(r.failures,0),COALESCE(r.spawn_epoch,0),
                COALESCE(r.handled_exit,0),COALESCE(r.spawned_at,0),o.retiring
                FROM owners o LEFT JOIN restart_policy r ON r.key=o.key
                WHERE o.key=?""", (key,)).fetchone()
            if row is None:
                return None
            return dict(key=row[0], desired=row[1], force_stop=bool(row[2]),
                        command_version=row[3], restart_failures=row[4],
                        spawn_epoch=row[5], handled_exit=row[6], spawned_at=row[7],
                        retiring=bool(row[8]))

    def launch_allowed(self, key, revision):
        """Read the exact desired-state revision authorizing a new worker.

        This read is intentionally distinct from the financial account fence:
        the worker must pass both the controller's startup gate AND the
        account-local transactional lease before executing any PAPER order.
        """
        if type(revision) is not int or revision < 0:
            return False
        with self.connection() as db:
            row = db.execute('SELECT desired,command_version FROM owners WHERE key=?',
                             (self._name(key),)).fetchone()
            return bool(row and row[0] == 'RUNNING' and row[1] == revision)

    def execution_authorized(self, key, revision):
        """Exact, durable authorization for one OS-worker incarnation.

        A STOP followed by START never reauthorizes an old worker, even
        though desired state has returned to RUNNING. A coordinator read
        error must propagate and prevent further financial operations.
        """
        return self.launch_allowed(key, revision)

    def desires_running(self, key):
        """Fail closed when a previously running worker has been stopped."""
        with self.connection() as db:
            row = db.execute('SELECT desired FROM owners WHERE key=?',
                             (self._name(key),)).fetchone()
            return bool(row and row[0] == 'RUNNING')

    def next_keys(self, cursor=None, *, limit=1):
        """Bounded keyset pagination, wrapping at the end of the registry."""
        if type(limit) is not int or limit < 1:
            raise ValueError('limit must be positive')
        with self.connection() as db:
            if cursor is None:
                first = [row[0] for row in db.execute('SELECT key FROM owners ORDER BY key LIMIT ?', (limit,))]
            else:
                first = [row[0] for row in db.execute('SELECT key FROM owners WHERE key>? ORDER BY key LIMIT ?', (cursor, limit))]
            if len(first) < limit:
                remaining = limit - len(first)
                start = first[-1] if first else cursor
                # Cursor can be beyond the final key; wrap to the first record.
                wrapped = [row[0] for row in db.execute('SELECT key FROM owners ORDER BY key LIMIT ?', (remaining,))]
                first += [key for key in wrapped if key not in first]
            return first

    def urgent_force_stops(self, cursor=None, *, limit):
        """Read a bounded priority slice independent of total fleet size.

        Force-stop intent remains durable until observed completion. Queries
        use an index, so urgent commands are not trapped behind a long
        ordinary registry sweep. The normal keyset scan still guarantees
        service for every registered bot.
        """
        if type(limit) is not int or limit < 1:
            raise ValueError('limit must be positive')
        with self.connection() as db:
            tail = [r[0] for r in db.execute(
                "SELECT key FROM owners WHERE force_stop=1 AND desired='STOPPED' "
                'AND key>? ORDER BY key LIMIT ?', (cursor or '', limit))]
            if len(tail) < limit:
                head = [r[0] for r in db.execute(
                    "SELECT key FROM owners WHERE force_stop=1 AND desired='STOPPED' "
                    'ORDER BY key LIMIT ?', (limit - len(tail),))]
                tail.extend(key for key in head if key not in tail)
            return tail

    def restart_status(self, key):
        with self.connection() as db:
            row = db.execute('SELECT failures,retry_after,last_error,spawn_epoch,handled_exit,spawned_at '
                             'FROM restart_policy WHERE key=?', (key,)).fetchone()
            fields = ('failures', 'retry_after', 'last_error', 'spawn_epoch', 'handled_exit', 'spawned_at')
            return dict(zip(fields, row)) if row else dict(
                failures=0, retry_after=0.0, last_error='',
                spawn_epoch=0, handled_exit=0, spawned_at=0.0)

    def record_spawn(self, key):
        """Durably identify a successfully submitted OS-worker generation."""
        with self._write() as db:
            if db.execute('SELECT 1 FROM owners WHERE key=?', (key,)).fetchone() is None:
                raise KeyError(key)
            db.execute("""INSERT INTO restart_policy(key,spawn_epoch,spawned_at)
                VALUES(?,1,?) ON CONFLICT(key) DO UPDATE SET
                spawn_epoch=spawn_epoch+1, spawned_at=excluded.spawned_at""",
                (key, float(self.clock())))

    def record_unexpected_exit(self, key, error, *, base_delay=2, max_delay=300):
        """Back off ONCE per observed worker generation, across host restarts."""
        with self._write() as db:
            row = db.execute('SELECT failures,retry_after,spawn_epoch,handled_exit '
                             'FROM restart_policy WHERE key=?', (key,)).fetchone()
            if not row or row[2] <= row[3]:
                return row[1] if row else 0.0
            failures = row[0] + 1
            delay = min(max_delay, base_delay * (2 ** min(failures - 1, 20)))
            retry_after = float(self.clock()) + delay
            db.execute('UPDATE restart_policy SET failures=?,retry_after=?,last_error=?,handled_exit=? WHERE key=?',
                       (failures, retry_after, str(error)[:2048], row[2], key))
            return retry_after

    def record_runtime_healthy(self, key, *, minimum_uptime=30):
        """Long-lived workers clear historical backoff without erasing exit tracking."""
        with self._write() as db:
            db.execute("""UPDATE restart_policy SET failures=0,retry_after=0,last_error=''
                WHERE key=? AND failures>0 AND spawn_epoch>handled_exit
                AND spawned_at>0 AND spawned_at<=?""",
                (key, float(self.clock()) - minimum_uptime))

    def record_restart_failure(self, key, error, *, base_delay=2, max_delay=300):
        with self._write() as db:
            if db.execute('SELECT 1 FROM owners WHERE key=?', (key,)).fetchone() is None:
                raise KeyError(key)
            row = db.execute('SELECT failures FROM restart_policy WHERE key=?', (key,)).fetchone()
            failures = (row[0] if row else 0) + 1
            delay = min(max_delay, base_delay * (2 ** min(failures - 1, 20)))
            retry_after = float(self.clock()) + delay
            db.execute("""INSERT INTO restart_policy(key,failures,retry_after,last_error)
                          VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET
                          failures=excluded.failures,retry_after=excluded.retry_after,
                          last_error=excluded.last_error""",
                       (key, failures, retry_after, str(error)[:2048]))
            return retry_after

    def clear_restart_failure(self, key, *, discard_previous_exit=False):
        """Explicit intent overrides delays, preserving current spawn identity."""
        with self._write() as db:
            db.execute("""UPDATE restart_policy SET failures=0,retry_after=0,last_error='',
                handled_exit=CASE WHEN ? THEN spawn_epoch ELSE handled_exit END
                WHERE key=?""", (int(bool(discard_previous_exit)), key))
