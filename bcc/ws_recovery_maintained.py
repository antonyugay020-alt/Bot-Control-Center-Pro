"""Secondary ticker chains staged privately until their correlated subscribe ACK.

A matching unsubscribe ACK permits a new chain; only genuine ordered frames
can initialize and advance it. The subscribe ACK releases, but never creates
or restamps, the evidence. No replay buffer or wall-clock price fallback.
"""
from .ws_recovery_quotes import RecoveryQuoteStream
from .ws_quotes import BybitQuoteStream, MaintainedQuoteStream, StreamUnavailable


class MaintainedRecoveryQuoteStream(RecoveryQuoteStream, MaintainedQuoteStream):
    def __init__(self, *, refresh_quiet=False, **kwargs):
        super().__init__(**kwargs)
        self._refresh_quiet = refresh_quiet
        self._refreshing_marks = {}
        self._received_topics = set()
        self._logical_demand = set()
        self._staging = set()
        self._early_snapshots = 0
        self._early_frames = 0
        self._released = 0
        self._empty_acks = 0
        self._empty_acks_at_connect = 0
        self._rotation_due = 0.
        self._snapshot_rotations = 0

    def _sync_chain(self):
        self._maintained.sync((self._logical_demand & self._subscriptions)
                              - (self._wire_symbols - self._staging))

    def connect(self, socket=None):
        with self._lock:
            self._staging.clear()
            self._refreshing_marks.clear()
            self._received_topics.clear()
            generation = super().connect(socket)
            self._empty_acks_at_connect = self._empty_acks
            self._sync_chain()
            return generation

    def disconnect(self, *, generation=None):
        with self._lock:
            if generation is not None and generation != self._generation:
                return False
            self._staging.clear()
            self._refreshing_marks.clear()
            self._received_topics.clear()
            return super().disconnect(generation=generation)

    def subscribe_many(self, symbols):
        with self._lock:
            previous = set(self._subscriptions)
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
            self._received_topics.difference_update(removed)
            for symbol in removed:
                self._refreshing_marks.pop(symbol, None)
            self._sync_chain()
            result = super().sync_active_symbols(symbols)
            self._sync_chain()
            self._notify_symbols(removed)
            return result

    def _send_recovery_requests(self, symbols):
        if self._refresh_quiet:
            for symbol in symbols:
                if self._maintained.mark(symbol) is not None:
                    self._refreshing_marks[symbol] = self._maintained.trackers[symbol]
        # Another topic may already await its subscribe ACK. Preserve
        # its staged chain while sending this independent unsubscribe.
        pending = self._wire_symbols - self._staging if self._refresh_quiet else self._wire_symbols
        self._maintained.sync((self._logical_demand & self._subscriptions)
                              - pending - set(symbols),
                              refreshing=symbols if self._refresh_quiet else ())
        for symbol in symbols:
            self._marks.pop(symbol, None)
            self._quotes.pop(symbol, None)
        self._notify_symbols(symbols)
        super()._send_recovery_requests(symbols)

    def request_quiet_snapshots(self, symbols):
        """Request actual snapshots with the existing correlated wire protocol."""
        with self._lock:
            # Run the normal ACK watchdog, independent of this request's budget.
            self.refresh_stale_marks([])
            if not self._connected or self._socket is None:
                return ()
            ready = tuple(symbol for symbol in symbols if symbol in self._logical_demand
                          and symbol in self._subscriptions and symbol not in self._wire_symbols)
            if ready:
                try:
                    self._send_recovery_requests(ready)
                except Exception as exc:
                    self._refresh_send_failures += 1
                    self._retire_uncertain_socket()
                    raise StreamUnavailable("Bybit quiet snapshot request failed") from exc
                self._refresh_topics_sent += len(ready)
                self._refresh_batches_sent += 1
            return ready

    def _send_wire(self, op, symbols):
        # Called for subscribe ONLY after the matching unsubscribe ACK.
        # Incoming callbacks use the same lock. Keep their validated state
        # private until subscribe succeeds, irrespective of frame/ACK order.
        if op == 'subscribe':
            self._staging.update(symbols)
            self._sync_chain()
        super()._send_wire(op, symbols)

    def _handle_control_message(self, socket, payload, generation):
        with self._lock:
            if generation != self._generation or socket is not self._socket:
                return True
            req_id = payload.get('req_id')
            pending = self._wire.get(req_id) if type(req_id) is str else None
            handled = super()._handle_control_message(socket, payload, generation)
            if not handled or generation != self._generation or not self._connected:
                return handled
            if pending is not None and pending[0] == 'subscribe':
                symbols = pending[1]
                self._staging.difference_update(symbols)
                self._sync_chain()
                released = []
                for symbol in symbols:
                    if symbol not in self._logical_demand:
                        continue
                    # Held evidence is never proof of the new subscription.
                    mark = self._maintained.mark(symbol)
                    if mark is None:
                        self._empty_acks += 1
                        continue
                    self._refreshing_marks.pop(symbol, None)
                    self._received_topics.add(symbol)
                    self._snapshot_recovery.observe_snapshot(symbol, (mark[3],))
                    self._released += 1
                    released.append(symbol)
                self._notify_symbols(released)
            else:
                self._sync_chain()
            return handled

    def ingest(self, payload, *, generation):
        with self._lock:
            if generation != self._generation or not self._connected:
                return False
            topic = payload.get('topic') if isinstance(payload, dict) else None
            if not isinstance(topic,str) or not topic.startswith('tickers.'):
                return False
            symbol = topic[8:]
            if symbol not in self._maintained.active:
                return False
            held = self._refreshing_marks.get(symbol)
            prior = (self._maintained.mark_from_tracker(symbol, held, self._connection_fence)
                     if held is not None else None)
            changed = self._maintained.ingest(payload, self._connection_fence, prior_snapshot=prior)
            current = self._maintained.mark(symbol)
            if symbol in self._wire_symbols:
                if symbol not in self._staging:
                    return False
                # No raw quote/mark publication, recovery completion or
                # consumer notification before a successful subscribe ACK.
                if changed and current is not None:
                    self._early_frames += 1
                    self._early_snapshots += int(payload.get('type') == 'snapshot')
                    return True
                return False
            if changed and current is not None:
                self._refreshing_marks.pop(symbol, None)
                self._received_topics.add(symbol)
                self._snapshot_recovery.observe_snapshot(symbol,(current[3],))
                BybitQuoteStream.ingest(self,payload,generation=generation)
            elif changed:
                self._marks.pop(symbol,None)
                self._quotes.pop(symbol,None)
            if changed:
                self._notify_symbols((symbol,))
            return changed

    def chain_initialized(self, symbol):
        """An intact but quiet chain is not a reason to destroy a subscription."""
        with self._lock:
            return (symbol in self._logical_demand and symbol not in self._wire_symbols
                    and self._maintained.has_chain(symbol))

    def rotate_after_empty_ack(self, symbols, *, interval=60.):
        """Retire only the reserve when resubscribe ACKs fail to yield snapshots.

        A new connection must obtain its own genuine snapshot. The caller
        ensures no other symbol currently depends on reserve-only evidence.
        No data or timestamp is inherited across the connection fence.
        """
        with self._lock:
            now = self._mono()
            if (not self._connected or self._socket is None or self._wire_symbols
                    or now < self._rotation_due
                    or self._empty_acks <= self._empty_acks_at_connect
                    or not any(s in self._logical_demand and not self._maintained.has_chain(s)
                               for s in symbols)):
                return False
            self._rotation_due = now + interval
            self._snapshot_rotations += 1
            socket = self._socket
            self.disconnect()
        socket.close()
        return True

    def maintained_mark(self, symbol):
        with self._lock:
            if symbol not in self._logical_demand:
                return None
            current = None if symbol in self._wire_symbols else self._maintained.mark(symbol)
            if current is not None:
                return current
            held = self._refreshing_marks.get(symbol)
            if held is not None:
                mark = self._maintained.mark_from_tracker(symbol, held, self._connection_fence)
                if mark is None:
                    self._refreshing_marks.pop(symbol, None)
                return mark
            return None

    def received_topic_count(self):
        with self._lock:
            return len(self._received_topics)

    def mark(self, symbol):
        mark = self.maintained_mark(symbol)
        if mark is None:
            raise StreamUnavailable('Bybit maintained recovery mark unavailable')
        return mark[:2]

    def refresh_pressure(self):
        with self._lock:
            return dict(super().refresh_pressure(), staged_topics=len(self._staging),
                        early_snapshots=self._early_snapshots, early_frames=self._early_frames,
                        released_on_ack=self._released, ack_without_fresh_mark=self._empty_acks,
                        snapshot_rotations=self._snapshot_rotations)
