"""Public Binance spot trade WebSocket for Polymarket PAPER impulse prices.

Does not alter initial market-start REST bar, indicator history, or settlement.
Uses no credentials, private endpoints or order channels.
"""
from __future__ import annotations
import json
import math
import re
import threading
import time
from dataclasses import dataclass
from .ws_quotes import StreamUnavailable

_SYMBOL = re.compile(r"[A-Z0-9]{2,24}USDT\Z")


@dataclass(frozen=True)
class TradePrice:
    symbol: str
    price: float
    exchange_ms: int
    monotonic_at: float
    generation: int


class BinanceTradeWS:
    ENDPOINT = "wss://stream.binance.com:9443/ws"
    MAX_SUB_PER_MSG = 25

    def __init__(self, *, wall=time.time, monotonic=time.monotonic,
                 max_age=2., on_update=None):
        if type(max_age) not in (int, float) or not math.isfinite(max_age) or not 0 < max_age <= 5:
            raise ValueError("invalid Binance price max_age")
        self._wall, self._mono, self._max_age = wall, monotonic, max_age
        self.on_update = on_update
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._generation = 0
        self._connected = False
        self._socket = None
        self._symbols = set()
        self._prices = {}

    def _invalidate(self):
        self._generation += 1
        self._connected = False
        self._socket = None
        self._prices.clear()

    def disconnect(self, *, generation=None):
        with self._lock:
            if generation is not None and generation != self._generation:
                return False
            self._invalidate()

    def connect(self, socket=None):
        with self._lock:
            if self._stop.is_set():
                raise StreamUnavailable("Binance stream stopped")
            self._invalidate()
            self._connected = True
            self._socket = socket
            return self._generation

    def _send(self, socket, symbols, operation):
        for i in range(0, len(symbols), self.MAX_SUB_PER_MSG):
            socket.send(json.dumps({
                "method": operation,
                "params": [symbol.lower() + "@trade"
                           for symbol in symbols[i:i + self.MAX_SUB_PER_MSG]],
                "id": i // self.MAX_SUB_PER_MSG + 1,
            }))

    def subscribe(self, symbols):
        if isinstance(symbols, str):
            symbols = [symbols]
        if not isinstance(symbols, (set, list, tuple)) or any(
                type(symbol) is not str or not _SYMBOL.fullmatch(symbol)
                for symbol in symbols):
            raise ValueError("invalid Binance symbols")
        with self._lock:
            added = sorted(set(symbols) - self._symbols)
            self._symbols.update(added)
            socket = self._socket if self._connected else None
            if socket and added:
                try:
                    self._send(socket, added, "SUBSCRIBE")
                except Exception as exc:
                    self._invalidate()
                    socket.close()
                    raise StreamUnavailable("Binance subscription failed") from exc

    def ingest(self, event, *, generation):
        if type(event) is not dict or event.get("e") != "trade":
            return False
        symbol = event.get("s")
        if type(symbol) is not str or not _SYMBOL.fullmatch(symbol):
            return False
        try:
            stamp = event["T"]
            price = float(event["p"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return False
        if (type(stamp) is not int or not math.isfinite(price) or price <= 0 or
                not -1 <= self._wall() - stamp / 1000 <= self._max_age):
            return False
        with self._lock:
            if (not self._connected or generation != self._generation or
                    symbol not in self._symbols):
                return False
            previous = self._prices.get(symbol)
            if previous and stamp < previous.exchange_ms:
                return False
            self._prices[symbol] = TradePrice(
                symbol, price, stamp, self._mono(), generation)
        if self.on_update:
            try:
                self.on_update("binance_trade", symbol)
            except Exception:
                pass
        return True

    def quote(self, symbol):
        with self._lock:
            value = self._prices.get(symbol)
            if (not self._connected or value is None or
                    value.generation != self._generation or
                    not 0 <= self._mono() - value.monotonic_at <= self._max_age or
                    not -1 <= self._wall() - value.exchange_ms / 1000 <= self._max_age):
                raise StreamUnavailable("Binance trade price absent or stale")
            return value.price, value.exchange_ms / 1000

    def run(self, *, websocket_app_factory=None, retry_delay=1.):
        if websocket_app_factory is None:
            from websocket import WebSocketApp
            websocket_app_factory = WebSocketApp
        self._stop.clear()
        while not self._stop.is_set():
            with self._lock:
                active = bool(self._symbols)
            if not active:
                self._stop.wait(.25)
                continue
            self.disconnect()
            state = {"generation": None}

            def opened(socket):
                if self._stop.is_set():
                    socket.close()
                    return
                state["generation"] = self.connect(socket)
                with self._lock:
                    self._send(socket, sorted(self._symbols), "SUBSCRIBE")

            def received(socket, raw):
                try:
                    data = json.loads(raw)
                except (TypeError, ValueError):
                    return
                self.ingest(data, generation=state["generation"])

            def closed(socket, code, reason):
                self.disconnect(generation=state["generation"])

            socket = websocket_app_factory(
                self.ENDPOINT, on_open=opened,
                on_message=received, on_close=closed)
            try:
                socket.run_forever(ping_interval=20, ping_timeout=10)
            except Exception:
                pass
            finally:
                self.disconnect(generation=state["generation"])
            self._stop.wait(retry_delay)

    def stop(self):
        self._stop.set()
        with self._lock:
            socket = self._socket
        self.disconnect()
        if socket is not None:
            socket.close()
