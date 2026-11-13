"""Public Polymarket CLOB market WebSocket orderbooks, fail closed.

The market channel uses public asset identifiers. Never uses user/API keys.
Snapshot + price_change are supported; unrecognized data never grants PAPER
trade permission. REST remains responsible for Gamma discovery and settlement.
"""
from __future__ import annotations
import json
import math
import re
import threading
import time
from dataclasses import dataclass
from .ws_quotes import StreamUnavailable

_TOKEN = re.compile(r"[0-9]{1,100}\Z")


@dataclass(frozen=True)
class ClobBook:
    asset_id: str
    bids: tuple
    asks: tuple
    timestamp_ms: int
    generation: int
    observed_mono: float


class PolymarketMarketWS:
    ENDPOINT = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    MAX_SUB_PER_MSG = 50

    def __init__(self, *, monotonic=time.monotonic, wall=time.time,
                 max_age=2., on_update=None):
        if type(max_age) not in (int, float) or not math.isfinite(max_age) or not 0 < max_age <= 5:
            raise ValueError("invalid CLOB book age")
        self._mono, self._wall, self._max_age = monotonic, wall, max_age
        self.on_update = on_update
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._generation = 0
        self._connected = False
        self._socket = None
        self._assets = set()
        self._books = {}

    def _invalidate(self):
        self._generation += 1
        self._connected = False
        self._socket = None
        self._books.clear()

    def disconnect(self, *, generation=None):
        with self._lock:
            if generation is not None and generation != self._generation:
                return False
            self._invalidate()

    def connect(self, socket=None):
        with self._lock:
            if self._stop.is_set():
                raise StreamUnavailable("CLOB socket stopped")
            self._invalidate()
            self._connected = True
            self._socket = socket
            return self._generation

    def subscribe(self, assets):
        if isinstance(assets, str):
            assets = [assets]
        if not isinstance(assets, (tuple, list, set)) or any(
                type(asset) is not str or not _TOKEN.fullmatch(asset)
                for asset in assets):
            raise ValueError("invalid Polymarket CLOB asset ids")
        with self._lock:
            additions = sorted(set(assets) - self._assets)
            self._assets.update(additions)
            socket = self._socket if self._connected else None
            if socket is not None and additions:
                try:
                    for i in range(0, len(additions), self.MAX_SUB_PER_MSG):
                        socket.send(json.dumps({
                            "assets_ids": additions[i:i + self.MAX_SUB_PER_MSG],
                            "operation": "subscribe",
                        }))
                except Exception as exc:
                    self._invalidate()
                    try:
                        socket.close()
                    finally:
                        raise StreamUnavailable("CLOB subscribe failed") from exc

    def sync_assets(self, assets):
        """Replace active CLOB subscriptions during 5m/15m market rotation.

        Old token books are invalidated immediately; an unsubscribed token
        may never authorize PAPER entry even while its previous cache TTL
        has not yet elapsed in a separate worker process.
        """
        if not isinstance(assets, (list, tuple, set)) or any(
                type(asset) is not str or not _TOKEN.fullmatch(asset)
                for asset in assets):
            raise ValueError("invalid Polymarket CLOB asset ids")
        desired = set(assets)
        with self._lock:
            removed = sorted(self._assets - desired)
            added = sorted(desired - self._assets)
            socket = self._socket if self._connected else None
            if socket is not None:
                try:
                    for kind, ids in (("unsubscribe", removed),
                                      ("subscribe", added)):
                        for i in range(0, len(ids), self.MAX_SUB_PER_MSG):
                            socket.send(json.dumps({
                                "assets_ids": ids[i:i + self.MAX_SUB_PER_MSG],
                                "operation": kind,
                            }))
                except Exception as exc:
                    self._invalidate()
                    try:
                        socket.close()
                    finally:
                        raise StreamUnavailable("CLOB market rotation failed") from exc
            self._assets = desired
            for token in removed:
                self._books.pop(token, None)

    @staticmethod
    def _levels(rows):
        if type(rows) is not list:
            raise ValueError("CLOB book levels not a list")
        result = {}
        for level in rows:
            if type(level) is not dict:
                raise ValueError("CLOB book level not an object")
            price = float(level["price"])
            size = float(level["size"])
            if (not math.isfinite(price) or not math.isfinite(size)
                    or not 0 < price < 1 or size < 0 or price in result):
                raise ValueError("CLOB invalid price, size or duplicate")
            if size:
                result[price] = size
        return result

    @staticmethod
    def _snapshot_sides(event):
        """Accept either documented CLOB depth field spelling, never both.

        Mixing `buys/sells` with `bids/asks` could hide contradictory
        liquidity. Ambiguous or partial snapshots must fail closed.
        """
        direct = "bids" in event or "asks" in event
        alternate = "buys" in event or "sells" in event
        if direct == alternate:
            raise ValueError("missing or ambiguous CLOB book side fields")
        if direct:
            if "bids" not in event or "asks" not in event:
                raise ValueError("incomplete CLOB bid/ask snapshot")
            return event["bids"], event["asks"]
        if "buys" not in event or "sells" not in event:
            raise ValueError("incomplete CLOB buys/sells snapshot")
        return event["buys"], event["sells"]

    @staticmethod
    def _book(asset, bids, asks, stamp, generation, now):
        buy = tuple(sorted(bids.items(), reverse=True))
        sell = tuple(sorted(asks.items()))
        if (not buy or not sell or len(buy) > 2000 or len(sell) > 2000
                or buy[0][0] >= sell[0][0]):
            raise ValueError("CLOB empty or crossed executable book")
        return ClobBook(asset, buy, sell, stamp, generation, now)

    def _fresh_timestamp(self, timestamp):
        if (type(timestamp) is not str or
                not timestamp.isdigit() or len(timestamp) > 16):
            raise ValueError("invalid CLOB event timestamp")
        value = int(timestamp)
        if not -1 <= self._wall() - value / 1000 <= self._max_age:
            raise ValueError("stale or future CLOB event")
        return value

    def ingest(self, event, *, generation):
        """Return True only for a fully validated book snapshot or delta."""
        if type(event) is not dict:
            return False
        kind = event.get("event_type")
        if kind not in ("book", "price_change"):
            return False
        if kind == "book":
            asset = event.get("asset_id")
            if type(asset) is not str or not _TOKEN.fullmatch(asset):
                return False
            try:
                stamp = self._fresh_timestamp(event["timestamp"])
                raw_bids, raw_asks = self._snapshot_sides(event)
                bids = self._levels(raw_bids)
                asks = self._levels(raw_asks)
            except (ValueError, KeyError, TypeError, OverflowError):
                with self._lock:
                    if generation == self._generation:
                        self._books.pop(asset, None)
                return False
            with self._lock:
                if (generation != self._generation or not self._connected
                        or asset not in self._assets):
                    return False
                before = self._books.get(asset)
                if before and stamp < before.timestamp_ms:
                    return False
                try:
                    current = self._book(asset, bids, asks, stamp,
                                         generation, self._mono())
                except ValueError:
                    self._books.pop(asset, None)
                    return False
                self._books[asset] = current
            if self.on_update:
                try:
                    self.on_update("polybook", asset)
                except Exception:
                    pass
            return True

        # price_change events can contain multiple tokens. Every item is
        # processed independently without accepting a partial, crossed book.
        changes = event.get("price_changes")
        if type(changes) is not list or not changes:
            return False
        try:
            stamp = self._fresh_timestamp(event["timestamp"])
        except (KeyError, ValueError):
            return False
        any_accepted = False
        for change in changes:
            if type(change) is not dict:
                continue
            asset = change.get("asset_id")
            if type(asset) is not str or not _TOKEN.fullmatch(asset):
                continue
            with self._lock:
                if generation != self._generation or not self._connected:
                    return any_accepted
                if asset not in self._assets:
                    continue
                before = self._books.get(asset)
                if before is None or stamp < before.timestamp_ms:
                    continue
                try:
                    price = float(change["price"])
                    size = float(change["size"])
                    side = change["side"]
                    if (not math.isfinite(price) or not math.isfinite(size)
                            or not 0 < price < 1 or size < 0
                            or side not in ("BUY", "SELL")):
                        raise ValueError("invalid CLOB change")
                    bids, asks = dict(before.bids), dict(before.asks)
                    levels = bids if side == "BUY" else asks
                    if size == 0:
                        levels.pop(price, None)
                    else:
                        levels[price] = size
                    current = self._book(asset, bids, asks, stamp,
                                         generation, self._mono())
                    for field, actual in (("best_bid", current.bids[0][0]),
                                          ("best_ask", current.asks[0][0])):
                        if field in change and float(change[field]) != actual:
                            raise ValueError("CLOB best price mismatch")
                except (ValueError, TypeError, KeyError, OverflowError):
                    self._books.pop(asset, None)
                    continue
                self._books[asset] = current
            any_accepted = True
            if self.on_update:
                try:
                    self.on_update("polybook", asset)
                except Exception:
                    pass
        return any_accepted

    def ingest_message(self, raw, *, generation):
        try:
            message = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            return False
        if isinstance(message, list):
            accepted = False
            for item in message:
                accepted = self.ingest(item, generation=generation) or accepted
            return accepted
        return self.ingest(message, generation=generation)

    def get_book(self, asset):
        with self._lock:
            book = self._books.get(asset)
            if (not self._connected or book is None or
                    book.generation != self._generation or
                    not 0 <= self._mono() - book.observed_mono <= self._max_age or
                    not -1 <= self._wall() - book.timestamp_ms / 1000 <= self._max_age):
                raise StreamUnavailable("Polymarket market book absent or stale")
            return {
                "asset_id": asset,
                "timestamp": str(book.timestamp_ms),
                "bids": [{"price": str(p), "size": str(q)} for p, q in book.bids],
                "asks": [{"price": str(p), "size": str(q)} for p, q in book.asks],
            }

    def run(self, *, websocket_app_factory=None, retry_delay=1.):
        if websocket_app_factory is None:
            from websocket import WebSocketApp
            websocket_app_factory = WebSocketApp
        if type(retry_delay) not in (int, float) or not .1 <= retry_delay <= 30:
            raise ValueError("invalid retry delay")
        self._stop.clear()
        while not self._stop.is_set():
            with self._lock:
                active = bool(self._assets)
            if not active:
                # No valid market to follow: avoid an empty CLOB handshake.
                self._stop.wait(.25)
                continue
            self.disconnect()
            state = {"generation": None}

            def opened(socket):
                if self._stop.is_set():
                    socket.close()
                    return
                generation = self.connect(socket)
                state["generation"] = generation
                with self._lock:
                    assets = sorted(self._assets)
                    if not assets:
                        self._invalidate()
                        socket.close()
                        return
                    socket.send(json.dumps({
                        "assets_ids": assets[:self.MAX_SUB_PER_MSG],
                        "type": "market",
                    }))
                    for i in range(self.MAX_SUB_PER_MSG, len(assets), self.MAX_SUB_PER_MSG):
                        socket.send(json.dumps({
                            "assets_ids": assets[i:i + self.MAX_SUB_PER_MSG],
                            "operation": "subscribe",
                        }))

            def received(socket, raw):
                try:
                    status = json.loads(raw)
                except (TypeError, ValueError):
                    status = None
                if (isinstance(status, dict) and
                        (status.get("success") is False
                         or status.get("event_type") == "error")):
                    self.disconnect(generation=state["generation"])
                    socket.close()
                    return
                self.ingest_message(raw, generation=state["generation"])

            def closed(socket, code, reason):
                self.disconnect(generation=state["generation"])

            socket = websocket_app_factory(
                self.ENDPOINT, on_open=opened,
                on_message=received, on_close=closed)
            pulse_stop = threading.Event()

            def heartbeat():
                # The CLOB market channel uses application-level PING.
                while not pulse_stop.wait(10):
                    with self._lock:
                        current = self._connected and self._socket is socket
                    if not current:
                        continue
                    try:
                        socket.send("PING")
                    except Exception:
                        self.disconnect(generation=state["generation"])
                        try:
                            socket.close()
                        except Exception:
                            pass
                        return

            pulse = threading.Thread(target=heartbeat, daemon=True,
                                     name="polymarket-market-heartbeat")
            pulse.start()
            try:
                socket.run_forever(ping_interval=20, ping_timeout=10)
            except Exception:
                pass
            finally:
                pulse_stop.set()
                pulse.join(timeout=2)
                self.disconnect(generation=state["generation"])
            self._stop.wait(retry_delay)

    def stop(self):
        self._stop.set()
        with self._lock:
            socket = self._socket
        self.disconnect()
        if socket is not None:
            socket.close()
