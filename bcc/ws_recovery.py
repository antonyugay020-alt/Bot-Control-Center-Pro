"""Bounded per-symbol WS snapshot recovery; evidence, not exchange ACK correlation."""

SNAPSHOT_RESPONSE_TIMEOUT = 1.25


class SnapshotRecovery:
    """Used under the owning stream lock; pending state follows subscription life.

    A new complete snapshot must strictly advance every independently timed
    field relative to the evidence captured before requesting recovery. A
    request or duplicate frame never grants freshness. A timeout only permits
    another bounded request; no market data is created or extended here.
    """

    def __init__(self):
        self._pending = {}
        self.completed = 0
        self.timed_out = 0

    def waiting(self, symbol, now):
        pending = self._pending.get(symbol)
        if pending is None:
            return False
        if now < pending[0]:
            return True
        self._pending.pop(symbol)
        self.timed_out += 1
        return False

    def begin(self, symbol, now, stamps):
        if symbol in self._pending:
            raise RuntimeError('Snapshot recovery already pending')
        self._pending[symbol] = (now + SNAPSHOT_RESPONSE_TIMEOUT, tuple(stamps))

    def extend_wait(self, symbol, now):
        """A wire ACK allows response time, never extends source price age."""
        pending = self._pending.get(symbol)
        if pending is not None:
            self._pending[symbol] = (max(pending[0], now + SNAPSHOT_RESPONSE_TIMEOUT), pending[1])

    def observe_snapshot(self, symbol, stamps):
        pending = self._pending.get(symbol)
        if pending is None:
            return False
        stamps = tuple(stamps)
        old = pending[1]
        if len(stamps) != len(old) or not all(new > prior for new, prior in zip(stamps, old)):
            return False
        self._pending.pop(symbol)
        self.completed += 1
        return True

    def pop(self, symbol, default=None):
        return self._pending.pop(symbol, default)

    def clear(self):
        self._pending.clear()

    def stats(self):
        return dict(pending_snapshot_topics=len(self._pending),
                    recovered_snapshot_topics=self.completed,
                    snapshot_response_timeouts=self.timed_out)
