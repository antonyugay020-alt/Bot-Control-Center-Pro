"""A lost account must never reappear in Qt as a fresh balance or empty bot."""
import shutil

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication

from bcc.storage import Store
from bcc.monitoring import RunningBotsWindow, snapshot_all
from bcc.ui import MainWindow
from test_portfolio_recovery_integration import STRATEGIES, account, advance


@pytest.mark.parametrize('strategy', STRATEGIES)
def test_qt_balance_details_and_reopened_window_share_recovery_boundary(tmp_path, strategy):
    app = QApplication.instance() or QApplication([])
    win = MainWindow(tmp_path / 'workspace')
    reopened = None
    monitor = None
    try:
        folder, cfg = account(win.repo, strategy, 'Recover ' + strategy)
        advance(folder, cfg)
        ledger = Store(folder)
        before, history = ledger.read(), ledger.history()
        backup = tmp_path / 'saved.sqlite'
        ledger.backup(backup)
        win.current = folder
        win.refresh()
        app.processEvents()
        assert win.positions.rowCount() == len(before['positions']) > 0
        (folder / 'paper.sqlite').unlink()
        win.refresh()
        app.processEvents()
        assert 'paper.sqlite' in win.bots_table.item(0, 3).text()
        assert win.bots_table.item(0, 4).text() == '—'
        assert 'Денежный баланс: —' in win.metrics.text()
        assert 'Открытых позиций: —' in win.metrics.text()
        assert win.positions.rowCount() == 0
        assert win.monitor_state is None
        assert 'paper.sqlite' in win.notice.text()
        assert not (folder / 'paper.sqlite').exists()
        monitor = RunningBotsWindow(win.repo, win.life, refresh_ms=100000)
        monitor._render(snapshot_all(win.repo, win.life))
        assert 'Позиций: —' in monitor.summary.text()
        assert monitor.bots.item(0, 8).text() == '—'
        reopened = MainWindow(win.repo.root)
        reopened.refresh()
        assert reopened.bots_table.item(0, 4).text() == '—'
        shutil.copyfile(backup, folder / 'paper.sqlite')
        reopened.current = folder
        reopened.refresh()
        app.processEvents()
        assert reopened.positions.rowCount() == len(before['positions'])
        assert reopened.bots_table.item(0, 4).text() == f"{before['cash']:.2f}"
        assert Store(folder).history() == history
        Store(folder).audit()
    finally:
        if monitor is not None:
            monitor.close()
            assert monitor.wait_for_snapshot_shutdown(10000)
            monitor.deleteLater()
        for window in (win, reopened):
            if window is not None:
                window.close()
                window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
