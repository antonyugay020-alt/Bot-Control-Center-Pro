"""Deterministic SQLite transaction AND connection lifecycle for BCC agent.

Python's "with sqlite3.connect(...)" only commits/rolls back; it does NOT
close the connection. On Windows, this keeps SQLite DB files locked.
Use this helper for short-lived connections needing a transaction, or use
contextlib.closing for explicitly managed transaction operations.

The helper creates no connection, changes no journal mode, and cannot
access a trading DB without a caller explicitly passing its connection.
Success and error paths deterministically close the connection.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import sqlite3
from typing import Iterator


@contextmanager
def managed_connection(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Commit/rollback AND guaranteed close (Windows-safe)."""
    with closing(connection) as conn:
        with conn:
            yield conn
