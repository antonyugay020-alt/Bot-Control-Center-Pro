"""Windows-safe SQLite lifecycle contract for the BCC developer gateway.

All files are synthetic and kept inside temporary test directories. The
tests never touch the user's persistent agent ledger or live BCC databases.
"""
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from tools.bcc_kie_sqlite_lifecycle import managed_connection


class ManagedSqliteLifecycleTests(unittest.TestCase):
    def test_success_commits_closes_and_allows_immediate_unlink(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "synthetic_success.sqlite3"
            conn = sqlite3.connect(path)
            with managed_connection(conn) as c:
                c.execute("CREATE TABLE records (value INTEGER)")
                c.execute("INSERT INTO records VALUES (17)")
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")
            with closing(sqlite3.connect(path)) as check:
                self.assertEqual(check.execute("SELECT value FROM records").fetchone()[0], 17)
            # Must succeed on Windows where an open handle prevents deletion.
            path.unlink()
            self.assertFalse(path.exists())

    def test_exception_rolls_back_and_closes_file(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "synthetic_rollback.sqlite3"
            with managed_connection(sqlite3.connect(path)) as initial:
                initial.execute("CREATE TABLE events (value INTEGER)")
            conn = sqlite3.connect(path)
            with self.assertRaisesRegex(RuntimeError, "synthetic"):
                with managed_connection(conn) as c:
                    c.execute("INSERT INTO events VALUES (99)")
                    raise RuntimeError("synthetic failure")
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")
            with managed_connection(sqlite3.connect(path)) as check:
                self.assertEqual(check.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
            path.unlink()

    def test_sql_error_closes_without_obscuring_exception(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "synthetic_error.sqlite3"
            conn = sqlite3.connect(path)
            with self.assertRaises(sqlite3.OperationalError):
                with managed_connection(conn) as c:
                    c.execute("SELECT * FROM missing_table")
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")
            path.unlink()

    def test_read_only_connection_also_closes(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "synthetic_readonly.sqlite3"
            with managed_connection(sqlite3.connect(path)) as init:
                init.execute("CREATE TABLE records (value INTEGER)")
                init.execute("INSERT INTO records VALUES (21)")
            before = path.read_bytes()
            uri = path.resolve().as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            with managed_connection(conn) as read:
                read.execute("PRAGMA query_only=ON")
                self.assertEqual(read.execute("SELECT value FROM records").fetchone()[0], 21)
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")
            self.assertEqual(before, path.read_bytes())
            path.unlink()

    def test_many_independent_connections_do_not_accumulate_locks(self):
        with TemporaryDirectory() as temp:
            for i in range(20):
                path = Path(temp) / ("synthetic_%02d.sqlite3" % i)
                with managed_connection(sqlite3.connect(path)) as db:
                    db.execute("CREATE TABLE t (id INTEGER)")
                    db.execute("INSERT INTO t VALUES (?)", (i,))
                path.unlink()
                self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
