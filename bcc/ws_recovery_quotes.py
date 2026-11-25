"""Secondary-only Bybit snapshot recovery with correlated control ACKs."""
from __future__ import annotations

import json
import uuid

from .ws_dispatch import MAX_REFRESH_TOPICS_PER_SCAN
from .ws_quotes import BybitQuoteStream, StreamUnavailable


class RecoveryQuoteStream(BybitQuoteStream):
    """Send subscribe only after success of its matching unsubscribe request.

    Logical snapshot evidence and wire command acknowledgement are independent.
    An uncertain command retires ONLY this recovery socket; the stable primary
    can keep consuming ordinary exchange deltas. Existing retry backoff applies.
    """
    ACK_TIMEOUT = 1.25

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._wire = {}
        self._wire_symbols = set()
        self._wire_acks = 0
        self._wire_failures = 0
        self._wire_timeouts = 0
        self._wire_subscribe_sent = 0

    def connect(self, socket=None):
        with self._lock:
            self._wire.clear()
            self._wire_symbols.clear()
            return super().connect(socket)

    def disconnect(self, *, generation=None):
        with self._lock:
            if generation is not None and generation != self._generation:
                return False
            self._wire.clear()
            self._wire_symbols.clear()
            return super().disconnect(generation=generation)

    def _retire_uncertain_socket(self):
        socket = self._socket
        self.disconnect()
        if socket is not None:
            socket.close()

    def sync_symbols(self, symbols):
        # A removal during a two-phase command must not be re-added by a
        # delayed ACK. Retire that secondary incarnation before reconciling.
        from .ws_symbols import USDT_SYMBOL
        if (not isinstance(symbols, (tuple, list, set)) or any(
                type(s) is not str or not USDT_SYMBOL.fullmatch(s) for s in symbols)):
            raise ValueError('invalid recovery symbols')
        with self._lock:
            if self._wire_symbols - set(symbols):
                self._retire_uncertain_socket()
            return super().sync_symbols(symbols)

    def sync_active_symbols(self, symbols):
        """Finish in-flight wire pairs before physically removing old demand.

        The dual parent revokes admission immediately. Only its secondary
        subscription persists until the correlated ACK is processed; the next
        registry scan then removes it. No delayed ACK can re-admit it.
        """
        with self._lock:
            retained = self._wire_symbols - set(symbols)
            for symbol in retained:
                self._marks.pop(symbol, None)
                self._quotes.pop(symbol, None)
            return super().sync_symbols(sorted(set(symbols) | retained))

    def _wire_refresh_pending(self, symbol):
        return symbol in self._wire_symbols

    def _send_wire(self, op, symbols):
        req_id = uuid.uuid4().hex
        self._wire[req_id] = (op, tuple(symbols), self._mono() + self.ACK_TIMEOUT)
        self._socket.send(json.dumps(dict(op=op, req_id=req_id,
            args=['tickers.'+s for s in symbols])))
        if op == 'subscribe':
            self._wire_subscribe_sent += len(symbols)

    def _send_recovery_requests(self, symbols):
        for start in range(0,len(symbols),self.MAX_SUB_PER_MSG):
            batch = symbols[start:start+self.MAX_SUB_PER_MSG]
            self._wire_symbols.update(batch)
            self._send_wire('unsubscribe',batch)

    def _handle_control_message(self, socket, payload, generation):
        with self._lock:
            if generation != self._generation or socket is not self._socket:
                return True
            req_id = payload.get('req_id')
            pending = self._wire.get(req_id) if type(req_id) is str else None
            if pending is None:
                return False
            op, symbols, deadline = pending
            if (self._mono() >= deadline or payload.get('op') != op
                    or payload.get('success') is not True):
                self._wire_failures += 1
                self._retire_uncertain_socket()
                return True
            self._wire.pop(req_id)
            self._wire_acks += 1
            if op == 'unsubscribe':
                try:
                    self._send_wire('subscribe',symbols)
                except Exception:
                    self._wire_failures += 1
                    self._retire_uncertain_socket()
            else:
                for symbol in symbols:
                    self._snapshot_recovery.extend_wait(symbol,self._mono())
                    self._wire_symbols.discard(symbol)
            return True

    def refresh_stale_marks(self, symbols, *, max_topics=MAX_REFRESH_TOPICS_PER_SCAN):
        with self._lock:
            if any(self._mono() >= deadline for _,_,deadline in self._wire.values()):
                self._wire_timeouts += 1
                self._retire_uncertain_socket()
                raise StreamUnavailable('Bybit recovery control ACK timeout')
            return super().refresh_stale_marks(symbols,max_topics=max_topics)

    def refresh_pressure(self):
        with self._lock:
            return dict(super().refresh_pressure(),
                        pending_wire_requests=len(self._wire),
                        pending_wire_topics=len(self._wire_symbols),
                        wire_acks=self._wire_acks,wire_failures=self._wire_failures,
                        wire_timeouts=self._wire_timeouts,
                        wire_subscribe_topics=self._wire_subscribe_sent)
