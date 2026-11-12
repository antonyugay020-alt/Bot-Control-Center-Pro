"""Deterministic control-plane SQLite ownership, independent of GC timing."""
import sqlite3
from contextlib import contextmanager, nullcontext

import pytest

from bcc.coordination import Coordinator
from bcc.storage import BotBusy


@pytest.mark.parametrize('failed', [False, True])
def test_control_connection_commits_or_rolls_back_then_always_closes(tmp_path, failed):
    control = Coordinator(tmp_path / 'coordination.sqlite')
    control.initialize()
    class InjectedFailure(RuntimeError):
        pass
    with pytest.raises(InjectedFailure) if failed else nullcontext():
        with control.connection() as database:
            database.execute('BEGIN IMMEDIATE')
            database.execute("INSERT INTO owners(key) VALUES('VELES/account')")
            if failed:
                raise InjectedFailure('rollback')
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        database.execute('SELECT 1')
    assert (control.get_registration('VELES/account') is not None) != failed


def test_control_and_ledger_fences_leave_no_handles_for_windows_teardown(tmp_path, monkeypatch):
    from test_architecture_control_ledger_commit_fence import managed_account
    held = []
    original = Coordinator.connection
    @contextmanager
    def traced(self):
        with original(self) as database:
            held.append(database)
            yield database
    monkeypatch.setattr(Coordinator, 'connection', traced)
    control, key, store = managed_account(tmp_path)
    with store.transaction() as (state, events):
        state['strategy']['durable'] = True
    control.set_desired(key, 'STOPPED')
    with pytest.raises(BotBusy, match='revision revoked'):
        with store.transaction() as (state, events):
            state['strategy']['illegal'] = True
    assert store.read()['strategy'] == {'durable': True}
    store.audit()
    assert held
    for database in held:
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            database.execute('SELECT 1')
    moved = control.path.with_name('control-moved.sqlite')
    control.path.rename(moved)
    moved.unlink()


def test_production_sqlite_connections_never_rely_on_context_exit_to_close():
    import ast
    from pathlib import Path
    for source in (Path(__file__).resolve().parents[1] / 'bcc').glob('*.py'):
        for node in ast.walk(ast.parse(source.read_text(encoding='utf-8'))):
            if isinstance(node, ast.With):
                for item in node.items:
                    call = item.context_expr
                    if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute):
                        assert not (isinstance(call.func.value, ast.Name)
                                    and call.func.value.id == 'sqlite3' and call.func.attr == 'connect'), (
                                        f'{source.name}:{node.lineno}: SQLite context does not close its handle')
