"""Hub registration, ownership and dequeue must not retain SQLite handles.

sqlite3.Connection's context-manager protocol does not close the connection.
The hub frequently creates short-lived connections; on Windows, unreleased
handles block clean Qt workspace teardown and can retain WAL resources.
"""

import sqlite3

import pytest

from bcc.market_hub import HubRegistry


URL = "https://api.bybit.com/v5/market/tickers"
PARAMS = {"category": "linear", "symbol": "BTCUSDT"}


def test_registry_connection_closes_after_success_and_error(tmp_path):
    registry = HubRegistry(tmp_path / "market.sqlite")

    with registry._connect() as db:
        assert db.execute("SELECT count(*) FROM hub_subscriptions").fetchone()[0] == 0

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        db.execute("SELECT 1")

    with pytest.raises(RuntimeError, match="rollback"):
        with registry._connect() as failed:
            failed.execute("BEGIN IMMEDIATE")
            failed.execute(
                "INSERT INTO hub_subscriptions"
                "(k,url,params,active_until,next_due,priority_until)"
                "VALUES('bad',?,?,0,0,0)",
                (URL, "{}",),
            )
            raise RuntimeError("rollback")

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        failed.execute("SELECT 1")

    with registry._connect() as fresh:
        assert fresh.execute(
            "SELECT count(*) FROM hub_subscriptions WHERE k='bad'"
        ).fetchone()[0] == 0


def test_hub_fleet_operations_leave_no_open_sqlite_connection(tmp_path):
    path = tmp_path / "market.sqlite"
    registry = HubRegistry(path)
    key = registry.subscribe(URL, PARAMS)
    assert registry.has_active_subscriptions()
    assert registry.claim_leader("producer")
    token = registry.leader_token("producer")
    assert token
    assert registry.due(limit=1) == [(key, URL, PARAMS)]
    assert registry.finish_owned(key, 1, "producer", token=token)
    assert registry.renew_leader("producer", token=token)
    registry.release_leader("producer", token=token)

    # Portable database ownership check, independently of unlink semantics:
    # all hub operations must have closed their short-lived connections.
    # On Windows deletion and replacement additionally fail with an open
    # SQLite handle. This final operation is intentionally an actual file move.
    destination = tmp_path / "market-moved.sqlite"
    path.rename(destination)
    assert destination.is_file()
