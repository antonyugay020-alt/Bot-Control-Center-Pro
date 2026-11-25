"""Maintained secondary ticker evidence with an explicit resubscription fence.

The two-phase wire ACK is NOT price evidence. A new genuine snapshot after
the retired timestamp is required before any delta can confirm the mark.
"""
from .ws_recovery_quotes import RecoveryQuoteStream
from .ws_quotes import MaintainedQuoteStream, StreamUnavailable


class MaintainedRecoveryQuoteStream(RecoveryQuoteStream, MaintainedQuoteStream):
    """Use independent same-topic state on the existing recovery socket."""

    def _sync_chain(self):
        self._maintained.sync(self._subscriptions - self._wire_symbols)

    def subscribe_many(self, symbols):
        with self._lock:
            super().subscribe_many(symbols)
            self._sync_chain()

    def sync_active_symbols(self, symbols):
        with self._lock:
            removed = self._maintained.active - set(symbols)
            self._maintained.sync(self._maintained.active - removed)
            result = super().sync_active_symbols(symbols)
            self._maintained.sync(set(symbols) - self._wire_symbols)
            self._notify_symbols(removed)
            return result

    def _send_recovery_requests(self, symbols):
        # Retire the local price chain BEFORE the unsubscribe is transmitted.
        # Old queued frames cannot re-authorize a retired subscription.
        self._maintained.sync(self._subscriptions - self._wire_symbols - set(symbols))
        for symbol in symbols:
            self._marks.pop(symbol, None)
            self._quotes.pop(symbol, None)
        self._notify_symbols(symbols)
        super()._send_recovery_requests(symbols)

    def _handle_control_message(self, socket, payload, generation):
        with self._lock:
            if generation != self._generation or socket is not self._socket:
                return True
            handled = super()._handle_control_message(socket, payload, generation)
            if handled:
                self._sync_chain()
            return handled

    def ingest(self, payload, *, generation):
        with self._lock:
            if generation != self._generation or not self._connected:
                return False
            topic = payload.get("topic") if isinstance(payload, dict) else None
            if isinstance(topic,str) and topic.startswith("tickers."):
                symbol = topic[8:]
                if symbol in self._wire_symbols or symbol not in self._maintained.active:
                    return False
            return super().ingest(payload, generation=generation)

    def mark(self, symbol):
        with self._lock:
            mark = self.maintained_mark(symbol)
            if mark is None or symbol in self._wire_symbols:
                raise StreamUnavailable("Bybit maintained recovery mark unavailable")
            return mark[:2]
