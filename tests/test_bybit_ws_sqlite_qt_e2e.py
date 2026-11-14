"""Bybit WS book -> fenced shared cache -> PAPER owner SQLite -> real Qt.

Deterministic offline transport. The real end-user native Windows run validates
the actual Qt widgets; no live exchange network, orders or user workspaces.
"""
import os
import time

if os.name != "nt" and not os.environ.get("DISPLAY"):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from bcc.config import validate
from bcc.market import PublicData
from bcc.market_hub import MarketDataHub
from bcc.market_clock import MarketClock, quote_is_recent
from bcc.position_report import position_mark_pnl
from bcc.runtime import process_tick
from bcc.scheduler import MarketTick
from bcc.storage import Repository, Store, read_paper_state
from bcc.strategies import Veles
from bcc.trading import enter, plan
from bcc.ui import MainWindow, apply_style
from test_core import attach
from test_veles_rsi_continuity import setup_positions

URL = "https://api.bybit.com/v5/market/orderbook"
PARAMS = {"category": "linear", "symbol": "BTCUSDT", "limit": 50}


class Stream:
    price = 101.0

    def get_orderbook(self, symbol):
        assert symbol == "BTCUSDT"
        return {
            "ts": time.time(),
            "bids": ((self.price - .01, 100.), (self.price - .02, 100.)),
            "asks": ((self.price + .01, 100.), (self.price + .02, 100.)),
            "u": 2, "seq": 3,
        }


def market_tick(source):
    value = source.get(URL, PARAMS)
    assert value["_bcc_ws_book"] is True
    data = value["result"]
    return MarketTick(
        symbol="BTCUSDT", bars=[], rates=[], funding_marks={},
        book={
            "at": data["ts"] / 1000,
            "bids": [[float(price), float(qty)] for price, qty in data["b"]],
            "asks": [[float(price), float(qty)] for price, qty in data["a"]],
            "qty_step": .0001, "min_qty": .0001,
            "min_notional": .01, "tick_size": .01, "max_qty": 100.,
        },
    )


def test_ws_book_reaches_persisted_live_qt_pnl_and_revokes_on_disconnection(tmp_path, monkeypatch):
    from bcc import market_clock
    clock = MarketClock()
    monkeypatch.setattr(market_clock, "clock", clock)
    monkeypatch.setattr("bcc.market.clock", clock)
    cfg, _, _, position_key = setup_positions()
    cfg["name"] = "WS SQLite Qt integration"
    cfg = validate(cfg)
    root = tmp_path / "isolated_paper_owner"
    repo = Repository(root)
    folder = repo.create(cfg)
    store = Store(folder, 500)
    with store.transaction() as (state, events):
        attach(state)
        section = cfg["sections"][0]
        ok, reason = enter(
            state, events, position_key, "BTCUSDT", "LONG",
            cfg["settings"], 100, 50,
            plan(cfg["settings"]["orders"], "LONG", 100), section["id"],
        )
        assert ok, reason
        state.pop("_books")
    store.audit()
    assert len(store.read()["positions"]) == 1

    source_root = tmp_path / "ws_hub"
    stream = Stream()
    producer = PublicData(orderbook_stream=stream)
    hub = MarketDataHub(source_root / "market_cache.sqlite", source=producer)
    app = QApplication.instance() or QApplication([])
    apply_style(app, {"theme": "dark", "scale": 100})
    window = None
    try:
        hub.registry.subscribe(URL, PARAMS)
        assert hub.acquire_leadership()
        monkeypatch.setenv("BCC_PUBLIC_CACHE", str(source_root / "market_cache.sqlite"))
        monkeypatch.setenv("BCC_MARKET_HUB_MODE", "client")
        monkeypatch.setenv("BCC_BYBIT_WS_BOOKS", "1")
        source = PublicData()
        try:
            monkeypatch.setattr(source.session, "get", lambda *a, **kw: (
                (_ for _ in ()).throw(AssertionError("REST forbidden"))
            ))
            assert hub.publish_ws_event("depth50", "BTCUSDT")
            process_tick(cfg, store, Veles(), market_tick(source))
            persisted = read_paper_state(folder)
            assert quote_is_recent(persisted["marks"]["BTCUSDT"], maximum=3)
            assert position_mark_pnl(persisted, persisted["positions"][position_key]) is not None

            window = MainWindow(root)
            window.show()
            window.current = folder
            window.refresh()
            window.refresh_live_positions()
            assert window.positions.rowCount() == 1
            assert "нет свежей оценки" not in window.positions.item(0, 6).text()

            # Consumer reports the WS outage, worker revokes stale valuation.
            process_tick(cfg, store, Veles(), MarketTick(
                "BTCUSDT", [], None, [], {}, quote_error="WebSocket disconnected"
            ))
            window.refresh_live_positions()
            assert window.positions.item(0, 6).text() == "— (нет свежей оценки)"

            # A new verified exchange snapshot restores valuation.
            stream.price = 102.
            assert hub.publish_ws_event("depth50", "BTCUSDT")
            process_tick(cfg, store, Veles(), market_tick(source))
            window.refresh_live_positions()
            assert window.positions.item(0, 6).text() != "— (нет свежей оценки)"
        finally:
            source.close()
    finally:
        if window is not None:
            window.live_positions_timer.stop()
            window.timer.stop()
            window.close()
            app.processEvents()
        hub.close()
        producer.close()
