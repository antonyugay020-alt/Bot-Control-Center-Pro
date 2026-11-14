"""Real Qt row updates from worker-state snapshots at subsecond cadence.

Transport is injected only at the read boundary; the table, row identity,
stale-value revocation and timer are the real production Qt components.
"""
import os
import time

if os.name != "nt" and not os.environ.get("DISPLAY"):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

from bcc.config import defaults
from bcc.storage import initial_state
from bcc.ui import MainWindow, apply_style, populate


def test_live_bybit_ws_pnl_updates_then_revokes_stale_snapshot(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    apply_style(app, {"theme": "dark", "scale": 100})
    window = MainWindow(tmp_path / "isolated_live_qt")
    cfg = defaults("VELES")
    cfg["name"] = "Bybit WS Qt Live"
    folder = window.supervisor.create_bot(cfg)
    state = initial_state(1000)
    state["positions"] = {
        "open:1": {
            "kind": "VELES", "symbol": "BTCUSDT",
            "section": "BTC", "side": "LONG", "qty": 2.0,
            "average": 100.0,
        },
    }
    state["marks"] = {
        "BTCUSDT": {"bid": 102.0, "ask": 103.0, "at": time.time()},
    }
    monkeypatch.setattr("bcc.ui.read_paper_state", lambda selected: state)
    monkeypatch.setattr("bcc.ui.available_margin", lambda snapshot: 100.)
    monkeypatch.setattr("bcc.ui.equity", lambda snapshot: (1000., False))
    try:
        window.show()
        window.current = folder
        window._details_folder = folder
        window._live_config_name = cfg["name"]
        window._live_config_strategy = "VELES"
        populate(window.positions, [["BTCUSDT", "BTC", "LONG", "2", "100",
                                    "50", "—", "0", "time"]], ["open:1"])
        window.refresh_live_positions()
        assert window.positions.item(0, 6).text() == "+4.0000"
        assert window.monitor_state is state
        assert "Bybit WS" in window.notice.text()

        # After a lost stream, old visual PnL must be revoked immediately,
        # never retained as a seemingly current position valuation.
        state["marks"]["BTCUSDT"]["at"] = time.time() - 20
        window.refresh_live_positions()
        assert window.positions.item(0, 6).text() == "— (нет свежей оценки)"
        assert "нет свежих исполнимых котировок" in window.notice.text()

        # A concurrent database read failure cannot preserve the old PnL.
        def broken(_):
            raise OSError("isolated SQLite read failed")
        monkeypatch.setattr("bcc.ui.read_paper_state", broken)
        window.refresh_live_positions()
        assert window.positions.item(0, 6).text() == "— (данные недоступны)"
    finally:
        window.live_positions_timer.stop()
        window.timer.stop()
        window.close()
        app.processEvents()
