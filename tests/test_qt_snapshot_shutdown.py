"""Qt monitoring readers must release SQLite handles before native QA cleanup."""

import os
import sqlite3
import threading

if os.name != "nt" and not os.environ.get("DISPLAY"):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import bcc.monitoring as monitoring


def test_native_monitor_snapshot_quiescence_releases_sqlite_before_cleanup(
    tmp_path, monkeypatch
):
    app = QApplication.instance() or QApplication([])
    db_file = tmp_path / "coordination.sqlite"
    db = sqlite3.connect(db_file)
    try:
        db.execute("CREATE TABLE snapshot (id INTEGER PRIMARY KEY)")
        db.commit()
    finally:
        db.close()
    started = threading.Event()
    release = threading.Event()

    def active_sqlite_snapshot(repo, life, include_stopped, include_history):
        db = sqlite3.connect(db_file, timeout=5)
        try:
            db.execute("SELECT count(*) FROM snapshot").fetchone()
            started.set()
            assert release.wait(10), "test failed to release the SQLite snapshot"
            return []
        finally:
            db.close()

    monkeypatch.setattr(monitoring, "snapshot_all", active_sqlite_snapshot)
    monitor = monitoring.RunningBotsWindow(object(), object(), refresh_ms=100000)
    try:
        monitor.show()
        assert started.wait(5), "reader was never scheduled"
        monitor.close()
        # A hidden dialog is NOT a stopped thread. The reader still owns the DB.
        assert not monitor.wait_for_snapshot_shutdown(1)
        release.set()
        assert monitor.wait_for_snapshot_shutdown(10000)
        assert monitor._snapshot_pool.activeThreadCount() == 0
    finally:
        release.set()
        monitor.wait_for_snapshot_shutdown(10000)
        monitor.close()
        monitor.deleteLater()
        app.processEvents()

    # On Windows this raises WinError 32 while the reader still owns the DB.
    db_file.unlink()
    assert not db_file.exists()
