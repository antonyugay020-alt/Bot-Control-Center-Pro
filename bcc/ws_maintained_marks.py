"""Opt-in WS mark reconstruction; separate explicit and confirmation clocks.

Only same-topic frames on the stable primary socket may confirm unchanged
fields. No ACK, other topic, local heartbeat or REST result can do so.
The caller owns synchronization and supplies verified exchange clock bounds.
"""
import math

from .ws_ticker_state import TickerState


class MaintainedMarks:
    def __init__(self, *, age_bounds, monotonic, utc_upper):
        self.age_bounds = age_bounds
        self.monotonic = monotonic
        self.utc_upper = utc_upper
        self.active = set()
        self.trackers = {}
        self.retired = {}
        self.watermarks = {}
        self.fence = None

    def reset(self, fence, symbols=()):
        self.fence = fence
        self.active = set(symbols) if fence else set()
        self.trackers.clear()
        self.retired.clear()
        self.watermarks.clear()

    def sync(self, symbols):
        selected = set(symbols) if self.fence else set()
        removed = self.active - selected
        if removed:
            try:
                upper = self.utc_upper()
                cutoff = math.ceil(upper * 1000) if math.isfinite(upper) else math.inf
            except (ValueError, TypeError, OverflowError, RuntimeError):
                cutoff = math.inf
            for symbol in removed:
                prior = self.watermarks.pop(symbol, (0, 0, 0.))
                self.retired[symbol] = max(cutoff, prior[0], self.retired.get(symbol, 0))
                self.trackers.pop(symbol, None)
        self.active = selected

    def ingest(self, payload, fence):
        if not self.fence or fence != self.fence or type(payload) is not dict:
            return False
        topic = payload.get('topic')
        if type(topic) is not str or not topic.startswith('tickers.'):
            return False
        symbol = topic[8:]
        if symbol not in self.active:
            return False
        stamp, cs = payload.get('ts'), payload.get('cs')
        if symbol not in self.trackers:
            if (type(stamp) is not int or stamp <= self.retired.get(symbol, 0)
                    or payload.get('type') != 'snapshot'):
                return False
            tracker = TickerState([symbol], age_bounds=self.age_bounds, monotonic=self.monotonic)
            self.trackers[symbol] = (tracker, tracker.connect())
        tracker, generation = self.trackers[symbol]
        previous = tracker.observations()[symbol]
        prior = self.watermarks.get(symbol)
        row = payload.get('data')
        conflict = False
        if prior is not None and type(stamp) is int and type(cs) is int:
            conflict = stamp < prior[0] or cs < prior[1]
            if stamp == prior[0] and type(row) is dict and 'markPrice' in row:
                try:
                    conflict = conflict or float(row['markPrice']) != prior[2]
                except (ValueError, TypeError, OverflowError):
                    conflict = True
        if conflict:
            tracker.reset_topics([symbol], generation)
            return previous is not None
        accepted = tracker.ingest(payload, generation)
        current = tracker.observations()[symbol]
        if accepted and current is not None:
            self.watermarks[symbol] = (current.confirmed_ms, current.sequence, current.price)
            return True
        return previous is not None and current is None

    def mark(self, symbol):
        selected = self.trackers.get(symbol)
        if not self.fence or symbol not in self.active or selected is None:
            return None
        state = selected[0].observations()[symbol]
        if state is None:
            return None
        return (state.price, state.confirmed_ms / 1000, self.fence,
                state.explicit_ms, state.sequence, state.chain)

    def stats(self):
        return dict(active=len(self.active), tracked=len(self.trackers),
                    fresh=sum(self.mark(s) is not None for s in self.trackers))


def maintained_metadata(value):
    """Legacy explicit records have no maintained-state fields."""
    keys = ('_bcc_ws_mark_basis', '_bcc_ws_mark_explicit_at',
            '_bcc_ws_mark_cs', '_bcc_ws_mark_chain')
    if not any(k in value for k in keys):
        return None
    stamp = value.get('_bcc_ws_mark_at')
    explicit, cs, chain = (value.get(k) for k in keys[1:])
    if (value.get(keys[0]) != 'snapshot_delta' or type(stamp) is not int
            or type(explicit) is not int or not 0 < explicit <= stamp
            or type(cs) is not int or cs < 0 or type(chain) is not int or chain <= 0
            or value.get('_bcc_ws_split_ticker') is not True):
        raise ValueError('Invalid maintained WS mark provenance')
    return explicit, cs, chain
