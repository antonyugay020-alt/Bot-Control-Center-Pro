"""Bounded recovery for an owned diagnostic socket only.

One batch in flight, explicit correlated unsubscribe/subscribe ACKs, and
no admission before the new subscription is acknowledged. Never used by BCC.
"""
import time
import uuid


class ProofRecovery:
    def __init__(self, tracker, generation, send, acked, *, monotonic=time.monotonic):
        self.tracker, self.generation = tracker, generation
        self.send, self.acked, self.now = send, acked, monotonic
        self.pending = None
        self.quarantined = set()
        self.attempts = {}
        self.next_due = 0.
        self.completed = 0
        self.awaiting_snapshot = {}
        self.restored = []
        self.snapshot_timeouts = 0
        self.events = []

    def _command(self, op, names):
        req = uuid.uuid4().hex
        self.pending = (op, req, tuple(names), self.now()+5.)
        self.send(dict(op=op, req_id=req, args=['tickers.'+s for s in names]))

    def poll(self):
        """At most 10 broken topics per 2s; at most two repairs per topic."""
        now = self.now()
        states = self.tracker.observations()
        for symbol, deadline in list(self.awaiting_snapshot.items()):
            state = states[symbol]
            if state is not None:
                self.restored.append(dict(symbol=symbol, chain=state.chain,
                    explicit_ms=state.explicit_ms, confirmed_ms=state.confirmed_ms))
                self.awaiting_snapshot.pop(symbol)
            elif now >= deadline:
                self.snapshot_timeouts += 1
                self.awaiting_snapshot.pop(symbol)
        if self.pending:
            if now >= self.pending[3]:
                raise RuntimeError('diagnostic recovery ACK timeout')
            return
        if now < self.next_due:
            return
        names = sorted((s for s in self.acked if states[s] is None
                        and s not in self.awaiting_snapshot
                        and self.attempts.get(s,0)<2),
                       key=lambda s:(self.attempts.get(s,0),s))[:10]
        if not names:
            return
        self.next_due = now+2.
        self.quarantined.update(names)
        self.acked.difference_update(names)
        if not self.tracker.reset_topics(names,self.generation):
            raise RuntimeError('diagnostic generation retired')
        for s in names:
            self.attempts[s] = self.attempts.get(s,0)+1
        self.events.append(dict(op='unsubscribe',symbols=names))
        self._command('unsubscribe',names)

    def acknowledge(self, message):
        if self.pending is None:
            return False
        op, req, names, deadline = self.pending
        if message.get('req_id') != req:
            return False
        if (self.now() >= deadline or message.get('op') != op
                or message.get('success') is not True):
            raise RuntimeError('diagnostic recovery ACK invalid')
        if op=='unsubscribe':
            # Data from the retiring subscription was quarantined. Only a
            # fresh snapshot can initialize the replacement maintained chain.
            self.quarantined.difference_update(names)
            self._command('subscribe',names)
        else:
            self.acked.update(names)
            self.pending = None
            self.completed += len(names)
            self.awaiting_snapshot.update({s:self.now()+3. for s in names})
            self.events.append(dict(op='subscribe_ack',symbols=list(names)))
            self.next_due = max(self.next_due,self.now()+2.)
        return True

    def allows_frame(self, symbol):
        return symbol not in self.quarantined

    def stats(self):
        return dict(attempts_by_symbol=dict(self.attempts),
                    acknowledged_resubscriptions=self.completed,
                    pending=self.pending[0] if self.pending else None,
                    restored_snapshots=list(self.restored),
                    awaiting_snapshot=sorted(self.awaiting_snapshot),
                    snapshot_timeouts=self.snapshot_timeouts,
                    events=list(self.events))
