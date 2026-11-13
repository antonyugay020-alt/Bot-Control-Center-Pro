"""One-minute Bybit linear kline WebSocket stream (current candle only).

REST remains responsible for historical backfill and continuity recovery.
Values never cross connection epochs; malformed or stale updates fail closed.
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from dataclasses import dataclass

from .ws_quotes import StreamUnavailable

_SYMBOL = re.compile(r"[A-Z0-9]{2,24}USDT\Z")


@dataclass(frozen=True)
class Candle:
    symbol: str
    start_ms: int
    end_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    confirmed: bool
    message_ms: int
    received_mono: float
    generation: int


class BybitKlineWS:
    ENDPOINT = "wss://stream.bybit.com/v5/public/linear"
    MAX_SUB_PER_MSG = 10

    def __init__(self, *, monotonic=time.monotonic, wall=time.time,
                 max_age=10., on_update=None):
        if (type(max_age) not in (int, float) or not math.isfinite(max_age)
                or not 0 < max_age <= 15):
            raise ValueError("invalid kline maximum age")
        self._mono, self._wall, self._max_age = monotonic, wall, max_age
        self.on_update = on_update
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._connected = False
        self._generation = 0
        self._symbols = set()
        self._socket = None
        self._candles = {}

    def subscribe(self, symbols):
        if isinstance(symbols, str):
            symbols = [symbols]
        if not isinstance(symbols, (list, tuple, set)) or any(
                type(symbol) is not str or not _SYMBOL.fullmatch(symbol)
                for symbol in symbols):
            raise ValueError("invalid kline symbols")
        with self._lock:
            new = sorted(set(symbols) - self._symbols)
            self._symbols.update(new)
            socket = self._socket if self._connected else None
            if socket is not None:
                try:
                    self._send(socket, new)
                except Exception as exc:
                    self._invalidate()
                    try:
                        socket.close()
                    finally:
                        raise StreamUnavailable("kline subscription failed") from exc

    def _send(self, socket, symbols):
        for offset in range(0, len(symbols), self.MAX_SUB_PER_MSG):
            socket.send(json.dumps({
                "op": "subscribe",
                "args": ["kline.1." + symbol
                         for symbol in symbols[offset:offset + self.MAX_SUB_PER_MSG]],
            }))

    def _invalidate(self):
        self._generation += 1
        self._connected = False
        self._socket = None
        self._candles.clear()

    def disconnect(self):
        with self._lock:
            self._invalidate()

    def connect(self, socket=None):
        with self._lock:
            if self._stop.is_set():
                raise StreamUnavailable("kline transport stopped")
            self._invalidate()
            self._connected = True
            self._socket = socket
            return self._generation

    def ingest(self, payload, *, generation):
        if not isinstance(payload, dict) or payload.get("type") != "snapshot":
            return False
        topic = payload.get("topic")
        if not isinstance(topic, str) or not topic.startswith("kline.1."):
            return False
        symbol = topic[len("kline.1."):]
        data = payload.get("data")
        if not isinstance(data, list) or len(data) != 1 or type(data[0]) is not dict:
            return False
        row = data[0]
        try:
            ts = payload["ts"]
            start = row["start"]
            end = row["end"]
            interval = row["interval"]
            confirmed = row["confirm"]
            op, hi, lo, cl, vol = (float(row[key]) for key in
                                   ("open", "high", "low", "close", "volume"))
            if (type(ts) is not int or type(start) is not int or type(end) is not int
                    or interval != "1" or type(confirmed) is not bool
                    or start % 60000 or end != start + 59999
                    or not -5 <= self._wall() - ts / 1000 <= self._max_age
                    or not -60 <= self._wall() * 1000 - start < 120000
                    or not all(math.isfinite(v) for v in (op, hi, lo, cl, vol))
                    or min(op, hi, lo, cl) <= 0 or vol < 0
                    or lo > min(op, cl) or hi < max(op, cl) or lo > hi):
                raise ValueError("invalid kline payload")
        except (KeyError, ValueError, TypeError, OverflowError):
            with self._lock:
                if self._connected and generation == self._generation:
                    self._candles.pop(symbol, None)
            return False
        with self._lock:
            if (not self._connected or generation != self._generation or
                    symbol not in self._symbols):
                return False
            previous = self._candles.get(symbol)
            if previous is not None and (
                    start < previous.start_ms or
                    (start == previous.start_ms and
                     (ts < previous.message_ms or
                      (previous.confirmed and not confirmed)))):
                return False
            self._candles[symbol] = Candle(
                symbol, start, end, op, hi, lo, cl, vol, confirmed,
                ts, self._mono(), generation,
            )
        if self.on_update is not None:
            try:
                self.on_update("kline1", symbol)
            except Exception:
                pass
        return True

    def get_candle(self, symbol):
        with self._lock:
            candle = self._candles.get(symbol)
            if (not self._connected or candle is None or
                    candle.generation != self._generation or
                    not 0 <= self._mono() - candle.received_mono <= self._max_age or
                    not -5 <= self._wall() - candle.message_ms / 1000 <= self._max_age):
                raise StreamUnavailable("Bybit minute candle absent or stale")
            return candle

    def run(self, *, websocket_app_factory=None, retry_delay=1.):
        if websocket_app_factory is None:
            try:
                from websocket import WebSocketApp
            except ImportError as exc:
                raise StreamUnavailable("websocket-client unavailable") from exc
            websocket_app_factory = WebSocketApp
        if (type(retry_delay) not in (int, float) or
                not math.isfinite(retry_delay) or not .1 <= retry_delay <= 30):
            raise ValueError("invalid retry delay")
        self._stop.clear()
        while not self._stop.is_set():
            self.disconnect()
            state = {"generation": None}

            def opened(socket):
                if self._stop.is_set():
                    socket.close()
                    return
                gen = self.connect(socket)
                state["generation"] = gen
                with self._lock:
                    self._send(socket, sorted(self._symbols))

            def received(socket, raw):
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    return
                if not isinstance(message, dict):
                    return
                if message.get("op") == "subscribe" and message.get("success") is False:
                    self.disconnect()
                    socket.close()
                    return
                self.ingest(message, generation=state["generation"])

            def closed(socket, code, reason):
                self.disconnect()

            socket = websocket_app_factory(
                self.ENDPOINT, on_open=opened, on_message=received,
                on_close=closed)
            try:
                socket.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:
                logging.warning("Bybit minute kline WS reconnect: %s",
                                type(exc).__name__)
            finally:
                self.disconnect()
            self._stop.wait(retry_delay)

    def stop(self):
        self._stop.set()
        with self._lock:
            socket = self._socket
        self.disconnect()
        if socket is not None:
            socket.close()
