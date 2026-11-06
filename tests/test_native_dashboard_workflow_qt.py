"""Native GUI -> controller -> independent PAPER worker -> monitor -> Stop/Restart.

Only the public transport is replayed. The actual Qt widgets, controller's
stop path, worker engine, SQLite ledger and monitor are exercised. This is not
proof of live-market operation or an independent Windows GUI acceptance run.
"""
import os
from threading import Event, Thread

if os.name != 'nt' and not os.environ.get('DISPLAY'):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton

from bcc.ui import MainWindow, apply_style
from bcc.storage import Store
from bcc.runtime_service import serve
from test_process import RUNNER, launch, setup_bot, wait_for


def click_action(app, win, caption):
    matches = [b for b in win.findChildren(QPushButton)
               if b.text() == caption and b.isVisible() and b.isEnabled()]
    assert len(matches) == 1, f'Expected one visible {caption!r} button, got {len(matches)}'
    QTest.mouseClick(matches[0], Qt.MouseButton.LeftButton)
    app.processEvents()


def test_dashboard_buttons_drive_real_paper_worker_monitor_and_restart(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    apply_style(app, {'scale': 100, 'theme': 'dark'})
    # Surface internal GUI errors immediately instead of hanging in a modal.
    def unexpected_warning(*args, **kwargs):
        pytest.fail('Dashboard displayed an unexpected warning: ' + repr(args[2:]))
    monkeypatch.setattr(QMessageBox, 'warning', unexpected_warning)
    win = MainWindow(tmp_path / 'isolated_qa_workspace')
    runner = tmp_path / 'controlled_public_data_worker.py'
    runner.write_text(RUNNER, encoding='utf-8')
    folder, _ = setup_bot(win.repo, runner, 'Native Dashboard PAPER cycle')
    # Only startup transport is redirected to an independently running replay
    # worker. Stop, process identity, state and GUI are the production paths.
    children = []
    service_stop = Event()
    service_failures = []
    def start_with_replayed_public_feed(actual_folder):
        assert actual_folder == folder
        child = launch(win.life, folder, runner)
        children.append(child)
        return child.pid
    monkeypatch.setattr(win.life, 'start', start_with_replayed_public_feed)
    def run_service():
        try:
            serve(win.repo.root, supervisor=win.supervisor, stop=service_stop,
                  interval=0.04, batch_size=3, inventory_interval=0.1)
        except BaseException as exc:
            service_failures.append(exc)
    service_thread = Thread(target=run_service, name='native-qa-runtime', daemon=True)
    service_thread.start()
    try:
        win.show()
        win.current = folder
        win.refresh()
        app.processEvents()

        assert win.selected() == folder
        assert win.bots_table.rowCount() == 1
        click_action(app, win, '▶ Запуск')
        # GUI only records intent; the real runtime service must start the
        # independent OS worker, never the button handler itself.
        wait_for(lambda: len(children) == 1 and children[0].poll() is None,
                 seconds=20)
        assert not service_failures
        wait_for(lambda: Store(folder).read()['positions'], seconds=20)
        win.refresh()
        assert win.positions.rowCount() == 1
        assert len(Store(folder).history()) == 1
        # Filtering the dashboard must not hide running bots in the global monitor.
        win.search.setText('definitely-no-match')
        click_action(app, win, 'Монитор всех ботов')
        monitor = win.running_monitor
        monitor.refresh()
        assert monitor.isVisible()
        deadline = __import__('time').monotonic() + 15
        while __import__('time').monotonic() < deadline:
            app.processEvents()
            if (not monitor._loading and
                    any(r['path'] == str(folder) and
                        r['status'] in ('Работает', 'Запуск')
                        for r in monitor.records)):
                break
            QTest.qWait(25)
        assert any(r['path'] == str(folder) and r['status'] in ('Работает', 'Запуск')
                   for r in monitor.records), monitor.alert.text()
        assert monitor.positions.rowCount() == 1
        monitor.close()
        win.search.clear()
        win.current = folder
        win.refresh()
        click_action(app, win, '■ Стоп')
        wait_for(lambda: children[-1].poll() is not None
                 and win.life.status(folder) == 'Остановлен', seconds=20)
        assert win.life.status(folder) == 'Остановлен'
        before = Store(folder).read()
        before_history = Store(folder).history()
        assert len(before['positions']) == len(before_history) == 1
        click_action(app, win, '▶ Запуск')
        wait_for(lambda: len(children) == 2 and children[-1].poll() is None,
                 seconds=20)
        wait_for(lambda: win.life.active_snapshot(folder), seconds=20)
        assert len(Store(folder).history()) == len(before_history), 'Duplicate entry on restart'
        click_action(app, win, '■ Стоп')
        wait_for(lambda: children[-1].poll() is not None
                 and win.life.status(folder) == 'Остановлен', seconds=20)
        state = Store(folder).read()
        from copy import deepcopy
        positions = deepcopy(state['positions'])
        old_positions = deepcopy(before['positions'])
        for key in old_positions:
            refreshed = positions[key]['margin_evidence'].pop('mark_at')
            previous = old_positions[key]['margin_evidence'].pop('mark_at')
            assert refreshed >= previous
        assert positions == old_positions
        assert state['cash'] == before['cash']
        Store(folder).audit()
        win.refresh()
        assert win.positions.rowCount() == 1, 'Stop must preserve existing PAPER position'
        assert win.history.rowCount() == 1
    finally:
        service_stop.set()
        service_thread.join(timeout=20)
        assert not service_thread.is_alive(), 'Runtime service did not stop'
        assert not service_failures, f'Runtime service crashed: {service_failures}'
        if win.running_monitor is not None:
            win.running_monitor.close()
        for child in children:
            if child.poll() is None:
                # Owned test subprocess only, never a user bot.
                try:
                    win.life.stop(folder, force=True)
                    child.wait(timeout=7)
                except Exception:
                    child.kill()
                    child.wait(timeout=7)
        win.close()
        win.deleteLater()
        app.processEvents()

def test_separate_qt_windows_reopen_running_account_inside_owned_acceptance_session(tmp_path):
    """Actual Qt interpreter exit/restart must not retire its trading runtime."""
    import json
    import subprocess
    import sys
    from pathlib import Path
    from bcc.runtime_health import observe
    from tools.acceptance_session import AcceptanceSession
    from tools.process_guard import run_logged_command

    script = tmp_path / 'native_window_probe.py'
    script.write_text('''
import json, os, sys
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox
import app
from bcc.storage import Store, atomic_json
root, folder, output = map(Path, sys.argv[1:4])
sys.argv = ['app.py', '--data-root', str(root)]
QMessageBox.question = lambda *_a, **_k: QMessageBox.StandardButton.Yes
BaseWindow = app.MainWindow
class CheckedWindow(BaseWindow):
 def show(self):
  super().show()
  QTimer.singleShot(350, self.record_and_close)
 def record_and_close(self):
  try:
   self.current = folder
   self.refresh()
   documents, notice = self.settings_documents(folder)
   process = self.life.active_snapshot(folder)
   assert process is not None, notice
   atomic_json(output, dict(status=self.life.status(folder), pid=process['pid'],
       config=documents['Действует в процессе'], positions=self.positions.rowCount(),
       history=len(Store(folder).history()), visible=self.isVisible(),
       native_windows=os.name == 'nt' and QApplication.instance().platformName() == 'windows'))
   self.close()
  except BaseException as exc:
   atomic_json(output, dict(error=type(exc).__name__ + ': ' + str(exc)))
   QApplication.instance().exit(1)
app.MainWindow = CheckedWindow
raise SystemExit(app.main())
''', encoding='utf-8')
    runner = tmp_path / 'controlled_public_worker.py'
    runner.write_text(RUNNER, encoding='utf-8')
    child = None
    with AcceptanceSession(tmp_path / 'qa_accounts', tmp_path / 'qa_session') as session:
        folder, _ = setup_bot(session.owner.repo, runner, 'Reopen working PAPER account')
        try:
            child = launch(session.owner.lifecycle, folder, runner,
                           accept_start=lambda: session.owner.request_start(folder))
            wait_for(lambda: Store(folder).read()['positions'], seconds=20)
            generation = observe(session.root)['run_id']
            history = Store(folder).history()
            before = Store(folder).read()
            for number in range(2):
                snapshot = tmp_path / f'window_{number}.json'
                with (tmp_path / f'window_{number}.log').open('w', encoding="utf-8") as log:
                    assert run_logged_command(
                        [sys.executable, str(script), str(session.root), str(folder), str(snapshot)],
                        cwd=Path(__file__).resolve().parents[1], stdout=log, timeout=40,
                        env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1])},
                    ) == 0, (tmp_path / f'window_{number}.log').read_text(errors='replace', encoding="utf-8")
                actual = json.loads(snapshot.read_text(encoding="utf-8"))
                assert actual.get('status') == 'Работает', actual
                assert actual['visible'] and actual['positions'] == 1 and actual['history'] == 1
                assert actual['config'] == session.owner.repo.load(folder)[0]
                if os.name == 'nt':
                    assert actual['native_windows'], actual
                assert child.poll() is None
                assert observe(session.root)['run_id'] == generation
                assert Store(folder).history() == history
                assert Store(folder).read()['cash'] == before['cash']
        except BaseException:
            if child is not None and child.poll() is None:
                session.owner.lifecycle.stop(folder, force=True)
                child.wait(timeout=10)
            raise
    assert child.wait(timeout=10) == 0
    assert observe(session.root) is None
    Store(folder).audit()

