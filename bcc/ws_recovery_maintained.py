"""Maintained secondary ticker evidence with an explicit resubscription fence.

The two-phase wire ACK is NOT price evidence. A new genuine snapshot after
the retired timestamp is required before any delta can confirm the mark.
"""
from .ws_recovery_quotes import RecoveryQuoteStream
from .ws_quotes import BybitQuoteStream, MaintainedQuoteStream, StreamUnavailable


class MaintainedRecoveryQuoteStream(RecoveryQuoteStream, MaintainedQuoteStream):
    """Use independent same-topic state on the existing recovery socket."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._logical_demand = set()

    def _sync_chain(self):
        self._maintained.sync(self._logical_demand & self._subscriptions - self._wire_symbols)

    def connect(self, socket=None):
        with self._lock:
            generation = super().connect(socket)
            self._sync_chain()
            return generation

    def subscribe_many(self, symbols):
        with self._lock:
            previous = set(self._subscriptions)
            # Do not temporarily activate pending wire topics through the
            # generic maintained subscription hook.
            BybitQuoteStream.subscribe_many(self, symbols)
            added = self._subscriptions - previous
            self._logical_demand.intersection_update(self._subscriptions)
            self._logical_demand.update(added)
            self._sync_chain()
            self._notify_symbols(added if self._connected else ())

    def sync_active_symbols(self, symbols):
        with self._lock:
            selected = set(symbols)
            removed = self._logical_demand - selected
            self._logical_demand = selected
            self._sync_chain()
            result = super().sync_active_symbols(symbols)
            self._sync_chain()
            self._notify_symbols(removed)
            return result

    def _send_recovery_requests(self, symbols):
        # Retire the local price chain BEFORE the unsubscribe is transmitted.
        # Old queued frames cannot re-authorize a retired subscription.
        self._maintained.sync((self._logical_demand & self._subscriptions)
                              - self._wire_symbols - set(symbols))
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
