"""Durable bot storage. Trading state and executions share one SQLite commit."""

import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
import time
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path

from .config import reject_private_data, validate
from .trade_fence import command_gate


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode(
        "utf-8"
    )


def load_json(path):
    return json.loads(
        Path(path).read_text(encoding="utf-8-sig"),
        parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)),
    )


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(json_bytes(value))
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(20):
            try:
                os.replace(name, path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05)
    finally:
        Path(name).unlink(missing_ok=True)


class BotBusy(RuntimeError):
    pass


class ControlRevoked(BotBusy):
    """Normal termination: the controller invalidated this launch epoch."""
    pass


class AccountingError(RuntimeError):
    """A broken persisted ledger or invariant stops the worker immediately."""


class BotLock:
    """OS releases the exclusive lease after normal exit, crash, or force stop."""

    def __init__(self, folder, *, allow_deletion_recovery=False):
        self.path = Path(folder) / "engine.lock"
        self.handle = None
        self.allow_deletion_recovery = allow_deletion_recovery

    def __enter__(self):
        if (self.path.parent.name.startswith((".creating-", ".deleted-", ".quarantined-"))
                or not import_committed(self.path.parent)):
            raise BotBusy("Незавершённая или удаляемая папка не может быть запущена")
        self.handle = self.path.open("a+b")
        self.handle.seek(0)
        if self.path.stat().st_size == 0:
            self.handle.write(b"0")
            self.handle.flush()
            self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError) as e:
            self.handle.close()
            self.handle = None
            raise BotBusy("Бот работает или выполняется другая операция") from e
        # Deletion is a two-stage rename on Windows: the OS lease must be
        # closed before renaming a folder. This durable marker closes the gap
        # between releasing the lease and moving the directory out of MY_BOTS.
        if (self.path.parent / ".deleting").exists() and not self.allow_deletion_recovery:
            self.__exit__(None, None, None)
            raise BotBusy("Бот удаляется; запуск или изменение запрещены")
        return self

    def __exit__(self, *args):
        if self.handle:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


def initial_state(capital):
    return dict(
        initial=capital,
        cash=capital,
        realized=0.0,
        positions={},
        pending={},
        clocks={},
        marks={},
        diagnostics={},
        stats={},
        done={},
        daily={},
        strategy={},
        created=time.time(),
    )


class Store:
    """PAPER ledger: once initialized, a missing database must never reset cash."""

    MARKER = "paper-account.json"

    def __init__(self, folder, capital=None):
        self.folder = Path(folder)
        self.path = self.folder / "paper.sqlite"
        self.marker = self.folder / self.MARKER
        if capital is not None:
            if self.path.exists():
                # Existing databases must have a valid ledger. Never repair an
                # empty/corrupt database by inserting a fresh initial state.
                self.audit()
                if not math.isclose(self.effective_capital(), capital, rel_tol=1e-10, abs_tol=1e-7):
                    raise AccountingError("Депозит не совпадает с PAPER-счётом")
            else:
                if self.marker.exists() or (self.folder / "process.json").exists():
                    raise AccountingError(
                        "Существующий PAPER-счёт исчез. Восстановите paper.sqlite из резервной копии"
                    )
                with self.connect(create=True) as db:
                    db.executescript(
                        "CREATE TABLE state(id INTEGER PRIMARY KEY CHECK(id=1), document TEXT NOT NULL); "
                        "CREATE TABLE executions(id TEXT PRIMARY KEY, at REAL NOT NULL, symbol TEXT NOT NULL, section TEXT NOT NULL, action TEXT NOT NULL, data TEXT NOT NULL);"
                    )
                    db.execute(
                        "INSERT INTO state VALUES(1,?)",
                        (json_bytes(initial_state(capital)).decode(),),
                    )
                self.audit()
            if self.marker.exists():
                try:
                    marker = load_json(self.marker)
                except (OSError, UnicodeError, ValueError) as exc:
                    raise AccountingError("Повреждён маркер существующего PAPER-счёта") from exc
                if (not isinstance(marker, dict) or marker.get("version") != 1 or
                        type(marker.get("initial")) not in (int, float) or
                        not math.isfinite(marker["initial"]) or
                        not math.isclose(marker["initial"], self.read(include_charts=False)["initial"], rel_tol=1e-10, abs_tol=1e-7) or
                        not isinstance(marker.get("account_id"), str) or
                        not marker["account_id"]):
                    raise AccountingError("Повреждён маркер существующего PAPER-счёта")
            else:
                atomic_json(self.marker, {
                    "version": 1, "account_id": uuid.uuid4().hex,
                    "initial": self.read(include_charts=False)["initial"],
                })

    @contextmanager
    def connect(self, *, create=False):
        # SQLite normally creates a blank database when a file is missing. All
        # reads, audits, transactions and backups MUST use mode=rw instead.
        location = (str(self.path) if create
                    else self.path.resolve().as_uri() + "?mode=rw")
        db = sqlite3.connect(location, timeout=10, isolation_level=None,
                             uri=not create)
        try:
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("PRAGMA synchronous=FULL")
            yield db
        finally:
            db.close()

    @staticmethod
    def _decode_document(raw):
        """Reject invalid SQLite JSON consistently, before touching PAPER state."""
        try:
            value = json.loads(raw, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
        except (TypeError, ValueError, UnicodeError) as exc:
            raise AccountingError("Повреждён JSON PAPER-счёта") from exc
        if not isinstance(value, dict):
            raise AccountingError("Повреждена структура PAPER-счёта")
        return value

    @staticmethod
    def _decode_event(raw):
        """A damaged journal must block all decisions, not crash at sum()."""
        event = Store._decode_document(raw)
        try:
            reject_private_data(event)
        except ValueError as exc:
            raise AccountingError("Недопустимые приватные данные в PAPER-журнале") from exc
        for field in ("cash_delta", "net_pnl"):
            value = event.get(field, 0 if field == "net_pnl" else None)
            if type(value) not in (int, float) or not math.isfinite(value):
                raise AccountingError("Повреждена денежная запись PAPER-журнала: " + field)
        return event

    @staticmethod
    def _decode_event_row(row):
        """Verify the durable index columns agree with the journal payload.

        Deposit recovery and effective_capital query SQLite columns. If a
        corrupted row advertises another action than its JSON payload, a
        legitimate contribution can silently disappear from available capital
        while the ordinary cash audit continues to pass. Fail closed before
        treating either representation as authoritative.
        """
        if not isinstance(row, (tuple, list)) or len(row) != 6:
            raise AccountingError("Повреждена строка PAPER-журнала")
        event_id, at, symbol, section, action, raw = row
        event = Store._decode_event(raw)
        if (type(event_id) is not str or not event_id or
                type(at) not in (int, float) or not math.isfinite(at) or at <= 0 or
                any(type(value) is not str or not value
                    for value in (symbol, section, action)) or
                event.get("id") != event_id or
                type(event.get("at")) not in (int, float) or
                not math.isfinite(event["at"]) or event["at"] != at or
                event.get("symbol") != symbol or
                event.get("section") != section or
                event.get("action") != action):
            raise AccountingError("Столбцы и содержимое PAPER-журнала не совпадают")
        return event

    @staticmethod
    def _decode_chart(raw):
        """Validate persisted chart rows on every read; never repair in place."""
        try:
            bars = json.loads(raw, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
        except (TypeError, ValueError, UnicodeError) as exc:
            raise AccountingError("Повреждён JSON истории графика PAPER") from exc
        if not isinstance(bars, list) or any(not isinstance(bar, dict) for bar in bars):
            raise AccountingError("Повреждена структура истории графика PAPER")
        check = initial_state(0)
        check["charts"] = {"_validated_chart": bars}
        Store.validate_state(check)
        return bars

    @staticmethod
    def _read_state_in_snapshot(db, include_charts=True):
        """Read the ledger document and chart rows in the same SQLite snapshot.

        Callers hold a read transaction before the first SELECT. A worker may
        commit concurrently; mixing the document from commit N with execution
        rows or charts from N+1 would produce a false corruption alarm.
        """
        row = db.execute("SELECT document FROM state WHERE id=1").fetchone()
        if not row:
            raise AccountingError("Отсутствует сохранённое PAPER-состояние")
        state = Store._decode_document(row[0])
        separate = state.pop("_chart_table", False)
        if type(separate) not in (bool, int) or separate not in (False, True):
            raise AccountingError("Повреждён маркер хранения PAPER-графиков")
        if separate:
            state["charts"] = (
                {
                    symbol: Store._decode_chart(data)
                    for symbol, data in db.execute("SELECT symbol,document FROM charts")
                }
                if include_charts
                else {}
            )
        elif not include_charts:
            Store.validate_state(state)  # Validate legacy charts before omitting.
            state["charts"] = {}
        Store.validate_state(state)
        return state

    def effective_capital(self):
        """Original funded principal plus separately journaled PAPER cash flows."""
        with self.connect() as db:
            db.execute("BEGIN")
            try:
                state = self._read_state_in_snapshot(db, include_charts=False)
                changes = []
                for row in db.execute("SELECT id,at,symbol,section,action,data FROM executions"):
                    event = self._decode_event_row(row)
                    if event["action"] == "CAPITAL_ADJUST":
                        changes.append(event["cash_delta"])
                return state["initial"] + math.fsum(changes)
            finally:
                db.rollback()

    def adjust_capital(self, amount, event_id):
        """Atomically journal a PAPER contribution/withdrawal, never trading PnL."""
        if type(amount) not in (int, float) or not math.isfinite(amount) or amount == 0:
            raise ValueError("Некорректная величина изменения депозита")
        if type(event_id) is not str or not event_id.strip() or len(event_id) > 128:
            raise ValueError("Некорректный идентификатор изменения депозита")
        from .trading import available_margin
        with self.transaction(include_charts=False) as (state, events):
            if amount < 0 and (not math.isfinite(available_margin(state))
                               or available_margin(state) + amount < -1e-7):
                raise ValueError("Недостаточно свободных средств для уменьшения депозита")
            if state["cash"] + amount < -1e-7:
                raise ValueError("Вывод превышает доступный PAPER-баланс")
            state["cash"] += amount
            # External funding must not be counted as profit/loss or trigger
            # false daily loss/drawdown warnings.
            if isinstance(state.get("daily"), dict) and "start" in state["daily"]:
                state["daily"]["start"] += amount
            if "equity_peak" in state:
                state["equity_peak"] = max(0.0, state["equity_peak"] + amount)
            events.append({
                "id": event_id, "at": time.time(), "symbol": "ACCOUNT",
                "section": "CAPITAL", "action": "CAPITAL_ADJUST",
                "cash_delta": amount, "net_pnl": 0.0,
            })

    def read(self, include_charts=True):
        with self.connect() as db:
            # Python's sqlite connection otherwise begins a fresh implicit
            # read for each SELECT, permitting a chart/document mismatch.
            db.execute("BEGIN")
            try:
                return self._read_state_in_snapshot(db, include_charts)
            finally:
                db.rollback()

    def chart_symbols(self):
        with self.connect() as db:
            db.execute("BEGIN")
            try:
                row = db.execute("SELECT document FROM state WHERE id=1").fetchone()
                if row is None:
                    raise AccountingError("Отсутствует сохранённое PAPER-состояние")
                state = self._decode_document(row[0])
                self.validate_state(state)
                if not state.get("_chart_table"):
                    charts = state.get("charts", {})
                    if not isinstance(charts, dict):
                        raise AccountingError("Повреждён формат сохранённых графиков")
                    return list(charts)
                return [
                    r[0] for r in db.execute("SELECT symbol FROM charts ORDER BY symbol")
                ]
            finally:
                db.rollback()

    def chart(self, symbol):
        with self.connect() as db:
            # Chart locations and contents must come from one committed
            # snapshot, including concurrent migration of old accounts.
            db.execute("BEGIN")
            try:
                row = db.execute("SELECT document FROM state WHERE id=1").fetchone()
                if row is None:
                    raise AccountingError("Отсутствует сохранённое PAPER-состояние")
                state = self._decode_document(row[0])
                self.validate_state(state)
                if state.get("_chart_table"):
                    chart_row = db.execute(
                        "SELECT document FROM charts WHERE symbol=?", (symbol,)
                    ).fetchone()
                    bars = self._decode_chart(chart_row[0]) if chart_row else []
                else:
                    bars = state.get("charts", {}).get(symbol, [])
                # A chart is public data, but imported values must still be
                # finite and free of private fields before rendering.
                check = initial_state(0)
                check["charts"] = {symbol: bars}
                self.validate_state(check)
                return bars
            finally:
                db.rollback()

    @staticmethod
    def validate_state(state):
        try:
            reject_private_data(state)
        except ValueError as e:
            raise AccountingError(str(e)) from e
        if not isinstance(state, dict) or not isinstance(state.get("positions"), dict):
            raise AccountingError("Повреждена структура PAPER-счёта")
        # Every module relies on these mappings being present. The previous
        # audit accepted arrays/scalars, only for a worker or Qt inspector to
        # crash later when it called .get(), .items(), .values() or .clear().
        for field in ("pending", "clocks", "marks", "diagnostics", "stats",
                      "done", "daily", "strategy"):
            if not isinstance(state.get(field), dict):
                raise AccountingError("Повреждена структура PAPER-счёта: " + field)
        for field in ("positions", "pending", "clocks", "marks", "stats",
                      "done", "diagnostics"):
            if any(not isinstance(key, str) for key in state[field]):
                raise AccountingError("Повреждены ключи PAPER-счёта: " + field)
        if any(not isinstance(p, dict) for p in state["pending"].values()):
            raise AccountingError("Повреждён ожидающий PAPER-ордер")
        archives = state.get('funding_archive', {})
        if (not isinstance(archives, dict)
                or any(not isinstance(key, str) or not isinstance(p, dict)
                       for key, p in archives.items())):
            raise AccountingError('Повреждён архив PAPER-позиций')
        for key in ("initial", "cash", "realized"):
            if type(state.get(key)) not in (int, float) or not math.isfinite(
                state[key]
            ):
                raise AccountingError("Повреждён PAPER-баланс")
        charts = state.get("charts", {})
        if (not isinstance(charts, dict) or
                any(not isinstance(symbol, str) or not isinstance(bars, list)
                    or any(not isinstance(bar, dict) for bar in bars)
                    for symbol, bars in charts.items())):
            raise AccountingError("Повреждена структура истории графиков PAPER")
        for key, p in state["positions"].items():
            if not isinstance(p, dict):
                raise AccountingError("Повреждена PAPER-позиция")
            from .liquidation import MarginUnavailable, validate_operation

            try:
                validate_operation(p)
            except (MarginUnavailable, KeyError, TypeError) as exc:
                raise AccountingError(str(exc)) from exc
            sides = (
                ("UP", "DOWN") if p.get("kind") == "POLYMARKET" else ("LONG", "SHORT")
            )
            if p.get("side") not in sides:
                raise AccountingError("Повреждено направление PAPER-позиции")
            for field in ("qty", "average", "margin", "order_margin"):
                if field == "order_margin" and field not in p:
                    continue
                value = p.get(field)
                if (
                    type(value) not in (int, float)
                    or not math.isfinite(value)
                    or (value <= 0 if field in ("qty", "average") else value < 0)
                ):
                    raise AccountingError("Повреждена позиция: " + field)

        def finite(node, depth=0):
            if depth > 40:
                raise AccountingError("Повреждена вложенность PAPER-состояния")
            if isinstance(node, float) and not math.isfinite(node):
                raise AccountingError("Неконечное число в PAPER-состоянии")
            if isinstance(node, dict):
                for v in node.values():
                    finite(v, depth + 1)
            elif isinstance(node, list):
                for v in node:
                    finite(v, depth + 1)

        finite(state)

    def audit(self):
        with self.connect() as db:
            # The balance and execution ledger MUST be read from the same
            # committed SQLite snapshot, even while a PAPER worker is writing.
            db.execute("BEGIN")
            try:
                state = self._read_state_in_snapshot(db)
                events = [
                    self._decode_event_row(row)
                    for row in db.execute("SELECT id,at,symbol,section,action,data FROM executions ORDER BY rowid")
                ]
            finally:
                db.rollback()
        delta = sum(e["cash_delta"] for e in events)
        realized = sum(e.get("net_pnl", 0) for e in events)
        if not math.isclose(
            state["initial"] + delta, state["cash"], rel_tol=1e-10, abs_tol=1e-7
        ):
            raise AccountingError(
                "Баланс не согласуется с журналом исполнений; запуск заблокирован"
            )
        if not math.isclose(realized, state["realized"], rel_tol=1e-10, abs_tol=1e-7):
            raise AccountingError("Результат не согласуется с журналом исполнений")
        self._reconcile_positions(state['positions'], events,
                                  archives=state.get('funding_archive', {}).values())

    @staticmethod
    def _reconcile_positions(positions, events, **baseline):
        from .position_journal import reconcile
        try:
            reconcile(positions, events, **baseline)
        except (ValueError, KeyError, TypeError, ZeroDivisionError) as exc:
            raise AccountingError('Нарушен журнал PAPER-позиций: ' + str(exc)) from exc

    @staticmethod
    def _position_baseline(state):
        from .position_journal import capture
        try:
            return capture(state['positions'], state.get('funding_archive', {}).values())
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise AccountingError('Нарушен журнал PAPER-позиций: ' + str(exc)) from exc

    def claim_execution(self, claimant, *, ttl=300, recovered_under_os_lock=False):
        """Acquire durable, account-local authority in the SAME DB as PAPER ledger.

        No external lease database is consulted when committing account events.
        A previous process cannot commit after a new generation has claimed.
        """
        if not isinstance(claimant, str) or not claimant.strip():
            raise ValueError("execution claimant must be nonempty")
        if not isinstance(ttl, (int, float)) or not math.isfinite(ttl) or ttl <= 0:
            raise ValueError("invalid execution lease")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("CREATE TABLE IF NOT EXISTS execution_owner "
                           "(id INTEGER PRIMARY KEY CHECK(id=1), generation INTEGER NOT NULL, "
                           "claimant TEXT, expires REAL NOT NULL)")
                db.execute("INSERT OR IGNORE INTO execution_owner VALUES(1,0,NULL,0)")
                generation, previous, expiry = db.execute(
                    "SELECT generation,claimant,expires FROM execution_owner WHERE id=1"
                ).fetchone()
                now = time.time()
                if previous is not None and expiry > now and not recovered_under_os_lock:
                    raise BotBusy("PAPER account is owned by an active execution")
                generation += 1
                db.execute("UPDATE execution_owner SET generation=?,claimant=?,expires=? WHERE id=1",
                           (generation, claimant, now + ttl))
                db.commit()
            except BaseException:
                db.rollback()
                raise
        self._execution_authority = (claimant, generation, ttl)
        return generation

    def release_execution(self):
        authority = getattr(self, "_execution_authority", None)
        if authority is None:
            return
        claimant, generation, _ = authority
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("UPDATE execution_owner SET claimant=NULL,expires=0 "
                           "WHERE id=1 AND claimant=? AND generation=?",
                           (claimant, generation))
                db.commit()
            except BaseException:
                db.rollback()
                raise
        self._execution_authority = None

    def _assert_execution_authority(self, db):
        authority = getattr(self, "_execution_authority", None)
        if authority is None:
            present = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_owner'").fetchone()
            if present:
                row = db.execute("SELECT claimant,expires FROM execution_owner WHERE id=1").fetchone()
                if row and row[0] is not None and row[1] > time.time():
                    raise BotBusy("Another executor owns the PAPER ledger")
            return
        claimant, generation, ttl = authority
        row = db.execute("SELECT claimant,generation,expires FROM execution_owner WHERE id=1").fetchone()
        now = time.time()
        if row is None or row[0] != claimant or row[1] != generation or row[2] <= now:
            raise BotBusy("Stale PAPER executor: transactional ledger commit denied")
        db.execute("UPDATE execution_owner SET expires=? WHERE id=1", (now + ttl,))

    def bind_control_authority(self, coordinator, owner_key, revision):
        """Bind a controlled worker's launch revision to every ledger commit.

        A legacy standalone Store (outside the managed runtime) is unaffected.
        The control gate is per-bot and is held only for ledger transactions;
        market fetches and strategy calculations outside transactions do not
        serialize unrelated bots.
        """
        if type(revision) is not int or revision < 0 or not isinstance(owner_key, str) or not owner_key:
            raise ValueError('invalid control authority')
        if not coordinator.launch_allowed(owner_key, revision):
            raise ControlRevoked('Control revision no longer authorizes PAPER commits')
        self._control_authority = (coordinator, owner_key, revision)

    def _assert_control_authority(self):
        authority = getattr(self, '_control_authority', None)
        if authority is not None:
            control, key, revision = authority
            if not control.launch_allowed(key, revision):
                raise ControlRevoked('Control revision revoked before PAPER commit')

    @contextmanager
    def transaction(self, include_charts=True):
        authority = getattr(self, '_control_authority', None)
        gate = command_gate(authority[0].path.parent, authority[1]) if authority else nullcontext()
        # Same gate is held across both SQLite operations and the final commit.
        # Therefore STOP and retirement cannot overtake an in-flight trade,
        # or let an old revision commit after STOP has been acknowledged.
        with gate, self.connect() as db:
            self._assert_control_authority()
            db.execute("BEGIN IMMEDIATE")
            try:
                self._assert_execution_authority(db)
                row = db.execute("SELECT document FROM state WHERE id=1").fetchone()
                if row is None:
                    raise AccountingError("Отсутствует сохранённое PAPER-состояние")
                state = self._decode_document(row[0])
                separate = state.pop("_chart_table", False)
                if type(separate) not in (bool, int) or separate not in (False, True):
                    raise AccountingError("Повреждён маркер хранения PAPER-графиков")
                legacy_charts = state.pop("charts", {})
                if not separate and legacy_charts:
                    check = initial_state(0)
                    check["charts"] = legacy_charts
                    self.validate_state(check)
                if separate and include_charts:
                    state["charts"] = {
                        symbol: Store._decode_chart(data)
                        for symbol, data in db.execute(
                            "SELECT symbol,document FROM charts"
                        )
                    }
                else:
                    state["charts"] = legacy_charts if include_charts else {}
                self.validate_state(state)
                before_cash = state["cash"]
                before_realized = state["realized"]
                # Validate an incremental asset journal inside the same SQLite
                # transaction as cash and executions; never rescan the complete
                # history on each market tick. Restart/import uses full replay.
                position_baseline = self._position_baseline(state)
                events = []
                yield state, events
                # Validate every proposed execution BEFORE arithmetic on its
                # cash fields; malformed inputs must roll back as a controlled
                # AccountingError, not an incidental TypeError halfway through.
                event_rows = []
                for event in events:
                    if not isinstance(event, dict):
                        raise AccountingError("Некорректное новое PAPER-исполнение")
                    try:
                        row = tuple(event[field] for field in
                                    ("id", "at", "symbol", "section", "action")) + (json_bytes(event).decode(),)
                    except (KeyError, ValueError, TypeError) as exc:
                        raise AccountingError("Некорректное новое PAPER-исполнение") from exc
                    self._decode_event_row(row)
                    event_rows.append(row)
                self.validate_state(state)
                self._reconcile_positions(state['positions'], events,
                                          baseline=position_baseline,
                                          archives=state.get('funding_archive', {}).values())
                if not math.isclose(
                    before_cash + sum(e.get("cash_delta", 0) for e in events),
                    state["cash"],
                    rel_tol=1e-10,
                    abs_tol=1e-7,
                ):
                    raise AccountingError(
                        "Денежное изменение без соответствующего исполнения"
                    )
                if state["realized"] != before_realized:
                    raise AccountingError(
                        "Результат изменён в обход журнала исполнений"
                    )
                state["realized"] = before_realized + sum(
                    e.get("net_pnl", 0) for e in events
                )
                self.validate_state(state)
                db.execute(
                    "CREATE TABLE IF NOT EXISTS charts(symbol TEXT PRIMARY KEY,document TEXT NOT NULL)"
                )
                charts = state.get("charts", {})
                if include_charts:
                    db.execute("DELETE FROM charts")
                elif not separate:
                    # Upgrade an old account in the same atomic commit. Legacy
                    # chart history is preserved, while new deltas take priority.
                    charts = {**legacy_charts, **charts}
                for symbol, bars in charts.items():
                    encoded = json.dumps(
                        bars, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                    )
                    db.execute(
                        "INSERT OR REPLACE INTO charts VALUES(?,?)", (symbol, encoded)
                    )
                document = {k: v for k, v in state.items() if k != "charts"}
                document["_chart_table"] = 1
                data = json.dumps(
                    document, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                )
                for row in event_rows:
                    db.execute("INSERT INTO executions VALUES(?,?,?,?,?,?)", row)
                db.execute("UPDATE state SET document=? WHERE id=1", (data,))
                self._assert_execution_authority(db)
                self._assert_control_authority()
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def history(self, limit=500):
        with self.connect() as db:
            return [
                self._decode_event_row(row)
                for row in db.execute(
                    "SELECT id,at,symbol,section,action,data FROM executions ORDER BY rowid DESC LIMIT ?", (limit,)
                )
            ]

    def backup(self, destination):
        with self.connect() as source:
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
            finally:
                target.close()


DEPOSIT_INTENT = ".deposit-change.json"


def recover_deposit_change(folder):
    """Complete an interrupted save using the already committed SQLite event.

    Caller MUST hold BotLock; only the exact event id in the durable intent
    authorizes publishing the corresponding configuration.
    """
    folder = Path(folder)
    intent_path = folder / DEPOSIT_INTENT
    if not intent_path.exists():
        return
    try:
        intent = load_json(intent_path)
    except (OSError, UnicodeError, ValueError) as exc:
        raise AccountingError("Повреждён журнал изменения депозита") from exc
    if (not isinstance(intent, dict) or intent.get("version") != 1 or
            not isinstance(intent.get("event_id"), str) or not intent["event_id"] or
            not isinstance(intent.get("old_config"), dict) or
            not isinstance(intent.get("new_config"), dict)):
        raise AccountingError("Повреждён журнал изменения депозита")
    old_cfg = validate(intent["old_config"])
    new_cfg = validate(intent["new_config"])
    present = validate(load_json(folder / "config.json"))
    if present != old_cfg and present != new_cfg:
        raise AccountingError("Настройки изменены во время изменения депозита")
    store = Store(folder)
    store.audit()
    with store.connect() as db:
        row = db.execute("SELECT data FROM executions WHERE id=?", (intent["event_id"],)).fetchone()
    if row:
        event = Store._decode_event(row[0])
        if (event.get("action") != "CAPITAL_ADJUST" or
                not math.isclose(event.get("cash_delta", float("nan")),
                                 new_cfg["capital"] - old_cfg["capital"],
                                 rel_tol=1e-10, abs_tol=1e-7)):
            raise AccountingError("Конфликт операции изменения депозита")
        if not math.isclose(store.effective_capital(), new_cfg["capital"], rel_tol=1e-10, abs_tol=1e-7):
            raise AccountingError("Депозит не совпадает с журналом операций")
        if present != new_cfg:
            atomic_json(folder / "config.json", new_cfg)
    else:
        if present != old_cfg:
            atomic_json(folder / "config.json", old_cfg)
    intent_path.unlink()


def guard_paper_recovery(folder):
    """Refuse modifications/exports if an initialized account lost its ledger."""
    folder = Path(folder)
    has_db = (folder / "paper.sqlite").is_file()
    known = ((folder / Store.MARKER).exists() or
             (folder / "process.json").exists())
    if known and not has_db:
        raise AccountingError(
            "PAPER-счёт ранее был создан, но paper.sqlite отсутствует. "
            "Восстановите базу из резервной копии"
        )
    if has_db:
        Store(folder).audit()
    return has_db


IMPORT_MARKER = ".import-batch"


def import_committed(folder, committed=None):
    """A whole imported batch becomes visible at one commit-file rename."""
    folder = Path(folder)
    marker = folder / IMPORT_MARKER
    if not marker.exists():
        return True
    try:
        batch = marker.read_text(encoding="ascii").strip()
        if not re.fullmatch(r"[0-9a-f]{32}", batch):
            return False
        if committed is not None:
            return batch in committed
        return (folder.parent.parent / ".import-commits" / batch).is_file()
    except (OSError, UnicodeError):
        return False


class Repository:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.bots = self.root / "MY_BOTS"
        self.bots.mkdir(parents=True, exist_ok=True)

    def resolve(self, folder):
        lexical = Path(os.path.abspath(folder))
        path = lexical.resolve(strict=True)
        if lexical != path or not path.is_relative_to(self.bots) or path == self.bots:
            raise ValueError("Папка бота за пределами MY_BOTS")
        if not import_committed(path):
            raise ValueError("Импорт бота не завершён; запуск запрещён")
        if any(p.is_symlink() for p in path.rglob("*")):
            raise ValueError("Ссылки в папке бота запрещены")
        return path

    def list(self):
        results = []
        # Capture one publication boundary for the entire dashboard list.
        # Otherwise a commit between two iterations could display only the
        # second half of an otherwise atomic multi-bot import.
        commits_dir = self.bots / ".import-commits"
        commits = frozenset(
            f.name for f in commits_dir.iterdir() if f.is_file()
        ) if commits_dir.is_dir() else frozenset()
        for file in sorted(self.bots.glob("*/*/config.json")):
            if file.parent.name.startswith((".creating-", ".deleted-", ".quarantined-")):
                continue
            if not import_committed(file.parent, commits):
                continue
            try:
                folder = self.resolve(file.parent)
                cfg = validate(load_json(file))
                results.append((folder, cfg))
            except (ValueError, OSError, KeyError, TypeError) as e:
                results.append(
                    (
                        file.parent,
                        {
                            "name": file.parent.name,
                            "strategy": file.parent.parent.name,
                            "invalid": str(e),
                        },
                    )
                )
        return results

    def iter_inventory(self):
        """Stream published bot configurations without a fleet-sized list.

        The commit-marker snapshot is fixed for one inventory pass. A bot that
        appears during traversal becomes eligible in the next pass. This
        iterator is intended for the independent runtime service; ``list``
        remains the explicit complete snapshot used by the dashboard.
        """
        commits_dir = self.bots / ".import-commits"
        commits = frozenset(
            item.name for item in commits_dir.iterdir() if item.is_file()
        ) if commits_dir.is_dir() else frozenset()
        for strategy in self.bots.iterdir():
            if not strategy.is_dir() or strategy.name.startswith("."):
                continue
            for folder in strategy.iterdir():
                if not folder.is_dir() or folder.name.startswith(
                    (".creating-", ".deleted-", ".quarantined-")
                ):
                    continue
                config = folder / "config.json"
                if not config.is_file() or not import_committed(folder, commits):
                    continue
                try:
                    resolved = self.resolve(folder)
                    yield resolved, validate(load_json(config))
                except (ValueError, OSError, KeyError, TypeError) as error:
                    yield folder, {
                        "name": folder.name,
                        "strategy": strategy.name,
                        "invalid": str(error),
                    }

    def prepare(self, cfg):
        """Create an unpublished bot in a hidden staging directory."""
        from .constructor_contract import validate_constructor_roundtrip
        cfg = validate_constructor_roundtrip(cfg)
        parent = self.bots / cfg["strategy"]
        parent.mkdir(exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".creating-", dir=parent))
        try:
            atomic_json(staging / "config.json", cfg)
            (staging / "engine.py").write_text(
                "from bcc.worker import main\nif __name__ == '__main__': main()\n",
                encoding="utf-8",
            )
            return staging
        except BaseException:
            import shutil
            shutil.rmtree(staging)
            raise

    def publish(self, staging):
        """Only make a completed bot visible in MY_BOTS after verification."""
        staging = Path(staging)
        if not staging.name.startswith(".creating-") or staging.parent.parent != self.bots:
            raise ValueError("Неизвестная папка подготовки бота")
        while True:
            target = staging.parent / uuid.uuid4().hex[:12]
            if not target.exists():
                staging.rename(target)
                return target

    def create(self, cfg):
        import shutil
        staged = self.prepare(cfg)
        try:
            return self.publish(staged)
        finally:
            if staged.exists():
                shutil.rmtree(staged)

    def load(self, folder):
        folder = self.resolve(folder)
        raw = (folder / "config.json").read_bytes()
        return validate(json.loads(raw)), hashlib.sha256(raw).hexdigest()

    def save(self, folder, cfg, expected):
        folder = self.resolve(folder)
        from .constructor_contract import validate_constructor_roundtrip
        cfg = validate_constructor_roundtrip(cfg)
        with BotLock(folder):
            from .launch_claim import check_launch_lease
            check_launch_lease(folder)
            recover_deposit_change(folder)
            old, digest = self.load(folder)
            if digest != expected:
                raise RuntimeError("Настройки изменились; откройте редактор повторно")
            if cfg["strategy"] != old["strategy"]:
                raise ValueError("Стратегия существующего бота не меняется")
            has_ledger = guard_paper_recovery(folder)
            if has_ledger:
                state = Store(folder).read()
                live = {
                    f"{s}:{sec['id']}"
                    for s in cfg["symbols"]
                    for sec in cfg["sections"]
                }
                for key, p in {**state["positions"], **state["pending"]}.items():
                    if (
                        p["symbol"] not in cfg["symbols"]
                        or cfg["strategy"]
                        in ("VELES", "EMA", "MACD", "BOLLINGER", "EMA_RSI_VOLUME")
                        and key not in live
                    ):
                        raise ValueError(
                            "Открытая позиция или ожидающая сетка требует сохранения монеты и секции"
                        )
            if has_ledger and cfg["capital"] != old["capital"]:
                store = Store(folder)
                if not math.isclose(store.effective_capital(), old["capital"], rel_tol=1e-10, abs_tol=1e-7):
                    raise AccountingError("Депозит не совпадает с журналом PAPER-операций")
                event_id = uuid.uuid4().hex
                atomic_json(folder / DEPOSIT_INTENT, {
                    "version": 1, "event_id": event_id,
                    "old_config": old, "new_config": cfg,
                })
                # SQLite commit is the transaction's durable decision point.
                # Recovery completes config publication if a crash occurs here.
                try:
                    store.adjust_capital(cfg["capital"] - old["capital"], event_id)
                    atomic_json(folder / "config.json", cfg)
                except BaseException:
                    recover_deposit_change(folder)
                    raise
                recover_deposit_change(folder)
            else:
                atomic_json(folder / "config.json", cfg)

    def copy(self, folder, *, history=False, name=None):
        import shutil

        folder = self.resolve(folder)
        with BotLock(folder):
            from .launch_claim import check_launch_lease
            check_launch_lease(folder)
            recover_deposit_change(folder)
            cfg, _ = self.load(folder)
            suffix = " — копия"
            cfg["name"] = (
                name if name is not None else cfg["name"][: 80 - len(suffix)] + suffix
            )
            has_ledger = guard_paper_recovery(folder)
            # Never expose a bot whose SQLite backup or logs are incomplete.
            # Prepare the entire independent bot before the atomic publication.
            staged = self.prepare(cfg)
            try:
                if history and has_ledger:
                    Store(folder).backup(staged / "paper.sqlite")
                    Store(staged, cfg["capital"])
                if history:
                    for log_name in ("engine.log", "console.log"):
                        if (folder / log_name).is_file():
                            shutil.copy2(folder / log_name, staged / log_name)
                return self.publish(staged)
            finally:
                if staged.exists():
                    shutil.rmtree(staged)

    def deletion_impact(self, folder):
        """Preview destructive PAPER data removal, even when its ledger is broken.

        An unreadable/missing previously initialized ledger is *not* repaired:
        the UI must warn about unknown contents and explicitly confirm deletion.
        Actual deletion rechecks the impact while holding the OS bot lease.
        """
        folder = self.resolve(folder)
        database = folder / "paper.sqlite"
        if database.is_file():
            try:
                state = Store(folder).read(include_charts=False)
                return {
                    "positions": len(state["positions"]),
                    "pending": len(state["pending"]),
                    "unknown": False,
                }
            except (OSError, sqlite3.Error, RuntimeError, ValueError,
                    TypeError, KeyError):
                return {"positions": None, "pending": None, "unknown": True}
        previously_initialized = (
            (folder / Store.MARKER).exists() or (folder / "process.json").exists()
        )
        return {"positions": 0, "pending": 0, "unknown": previously_initialized}

    def delete(self, folder, *, discard_paper_state=False):
        """Delete stopped bot after an explicit UI decision to discard its PAPER data.

        Stop does not erase positions; Delete intentionally removes the account,
        open positions, pending orders and history. A worker must never be active.
        """
        import shutil

        folder = self.resolve(folder)
        marker = folder / ".deleting"
        # The marker persists across the unavoidable unlocked Windows rename.
        # It prevents any controller or worker from acquiring a new BotLock.
        # A crash after publishing .deleting but before the rename must not
        # permanently strand the bot. Only the destructive delete path can
        # recover the marker; trading and ordinary writes stay fenced.
        with BotLock(folder, allow_deletion_recovery=True):
            from .launch_claim import check_launch_lease
            check_launch_lease(folder)
            if marker.exists():
                try:
                    previous = load_json(marker)
                except (OSError, ValueError, TypeError) as exc:
                    raise AccountingError("Повреждён маркер незавершённого удаления") from exc
                if (not isinstance(previous, dict)
                        or previous.get('operation') != 'delete'
                        or not isinstance(previous.get('id'), str)
                        or not previous['id']):
                    raise AccountingError("Недостоверный маркер удаления")
            impact = self.deletion_impact(folder)
            if (impact["positions"] or impact["pending"] or impact["unknown"]):
                if not discard_paper_state:
                    raise ValueError(
                        "Для удаления бота с PAPER-позициями, ордерами или "
                        "непроверенным счётом требуется явное подтверждение "
                        "безвозвратного удаления всех данных"
                    )
            atomic_json(marker, {"operation": "delete", "id": uuid.uuid4().hex})
        tomb = folder.with_name(".deleted-" + uuid.uuid4().hex)
        try:
            folder.rename(tomb)
        except BaseException:
            # The original folder has not moved; cancel the deletion marker.
            # A successful move is a commit point and must not be undone after
            # rmtree starts because a partial removal cannot safely be undone.
            marker.unlink(missing_ok=True)
            raise
        try:
            shutil.rmtree(tomb)
        except BaseException as exc:
            # Preserve anything still remaining for manual recovery. Do not
            # expose an incomplete account under its old bot ID.
            raise AccountingError(
                "Удаление прервалось после перемещения бота; остатки "
                f"изолированы в {tomb}. Проверьте их перед восстановлением"
            ) from exc
