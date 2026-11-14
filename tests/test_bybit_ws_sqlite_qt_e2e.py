"""Bybit WS book -> fenced shared cache -> PAPER owner SQLite -> real Qt.

Deterministic offline transport. The real end-user native Windows run validates
the actual Qt widgets; no live exchange network, orders or user workspaces.
"""
import os
import time
import pytest

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
from bcc.ws_quotes import BybitQuoteStream
from bcc.margin import snapshot, MarginUnavailable
from test_margin_finance import use_mark
from test_core import attach
from test_veles_rsi_continuity import setup_positions

URL = "https://api.bybit.com/v5/market/orderbook"
PARAMS = {"category": "linear", "symbol": "BTCUSDT", "limit": 50}


class Stream:
    price = 101.0

    def get_orderbook(self, symbol):
        return {
            "ts": time.time(),
            "bids": ((self.price - .01, 100.), (self.price - .02, 100.)),
            "asks": ((self.price + .01, 100.), (self.price + .02, 100.)),
            "u": 2, "seq": 3,
        }


def market_tick(source, symbol):
    value = source.get(URL, dict(PARAMS, symbol=symbol))
    assert value["_bcc_ws_book"] is True
    data = value["result"]
    return MarketTick(
        symbol=symbol, bars=[], rates=[], funding_marks={},
        book={
            "at": data["ts"] / 1000,
            "bids": [[float(price), float(qty)] for price, qty in data["b"]],
            "asks": [[float(price), float(qty)] for price, qty in data["a"]],
            "margin_required": True,
            "margin_marks": {symbol: source._ws_position_mark(symbol)},
            "qty_step": .0001, "min_qty": .0001,
            "min_notional": .01, "tick_size": .01, "max_qty": 100.,
        },
    )


@pytest.mark.parametrize("symbol", ["BTCUSDT", "ADAUSDT"])
def test_ws_book_reaches_persisted_live_qt_pnl_and_revokes_on_disconnection(tmp_path, monkeypatch, symbol):
    from bcc import market_clock
    clock = MarketClock()
    monkeypatch.setattr(market_clock, "clock", clock)
    monkeypatch.setattr("bcc.market.clock", clock)
    cfg, _, _, position_key = setup_positions()
    cfg["symbols"] = [symbol]
    position_key = position_key.replace("BTCUSDT", symbol)
    cfg["name"] = "WS SQLite Qt integration"
    cfg = validate(cfg)
    root = tmp_path / "isolated_paper_owner"
    repo = Repository(root)
    folder = repo.create(cfg)
    store = Store(folder, 500)
    with store.transaction() as (state, events):
        attach(state, symbol=symbol)
        section = cfg["sections"][0]
        ok, reason = enter(
            state, events, position_key, symbol, "LONG",
            cfg["settings"], 100, 50,
            plan(cfg["settings"]["orders"], "LONG", 100), section["id"],
        )
        assert ok, reason
        use_mark(state, symbol=symbol)
        state.pop("_books")
    store.audit()
    assert len(store.read()["positions"]) == 1

    source_root = tmp_path / "ws_hub"
    stream = Stream()
    quotes = BybitQuoteStream()
    quotes.subscribe(symbol)
    generation = quotes.connect()
    def exchange_snapshot(mark, generation):
        assert quotes.ingest({
            "type": "snapshot", "topic": "tickers." + symbol,
            "ts": int(time.time() * 1000),
            "data": {"symbol": symbol, "bid1Price": "100", "ask1Price": "101",
                     "markPrice": str(mark)},
        }, generation=generation)
    producer = PublicData(orderbook_stream=stream, quote_stream=quotes)
    hub = MarketDataHub(source_root / "market_cache.sqlite", source=producer)
    app = QApplication.instance() or QApplication([])
    apply_style(app, {"theme": "dark", "scale": 100})
    window = None
    try:
        hub.registry.subscribe(URL, dict(PARAMS, symbol=symbol))
        hub.registry.subscribe("https://api.bybit.com/v5/market/tickers",
                               {"category": "linear", "symbol": symbol})
        assert hub.acquire_leadership()
        monkeypatch.setenv("BCC_PUBLIC_CACHE", str(source_root / "market_cache.sqlite"))
        monkeypatch.setenv("BCC_MARKET_HUB_MODE", "client")
        monkeypatch.setenv("BCC_BYBIT_WS_BOOKS", "1")
        monkeypatch.setenv("BCC_BYBIT_WS_QUOTES", "1")
        source = PublicData()
        try:
            monkeypatch.setattr(source.session, "get", lambda *a, **kw: (
                (_ for _ in ()).throw(AssertionError("REST forbidden"))
            ))
            exchange_snapshot(100.4, generation)
            assert hub.publish_ws_event("tickers", symbol)
            assert hub.publish_ws_event("depth50", symbol)
            process_tick(cfg, store, Veles(), market_tick(source, symbol))
            persisted = read_paper_state(folder)
            assert quote_is_recent(persisted["marks"][symbol], maximum=3)
            assert position_mark_pnl(persisted, persisted["positions"][position_key]) is not None

            assert snapshot(persisted, symbol)[1] == 100.4
            store.audit()
            window = MainWindow(root)
            window.show()
            window.current = folder
            window.refresh()
            window.refresh_live_positions()
            assert window.positions.rowCount() == 1
            assert "нет свежей оценки" not in window.positions.item(0, 6).text()

            # Consumer reports the WS outage, worker revokes stale valuation.
            quotes.disconnect()
            process_tick(cfg, store, Veles(), MarketTick(
                symbol, [], None, [], {}, quote_error="WebSocket disconnected"
            ))
            window.refresh_live_positions()
            assert window.positions.item(0, 6).text() == "— (нет свежей оценки)"
            with pytest.raises(MarginUnavailable):
                snapshot(read_paper_state(folder), symbol)

            # A new verified exchange snapshot restores valuation.
            stream.price = 102.
            generation = quotes.connect()
            exchange_snapshot(101.4, generation)
            assert hub.publish_ws_event("tickers", symbol)
            assert hub.publish_ws_event("depth50", symbol)
            process_tick(cfg, store, Veles(), market_tick(source, symbol))
            window.refresh_live_positions()
            assert window.positions.item(0, 6).text() != "— (нет свежей оценки)"
            reopened = Store(folder)
            assert snapshot(reopened.read(), symbol)[1] == 101.4
            assert len(reopened.history()) == 1
            reopened.audit()
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
