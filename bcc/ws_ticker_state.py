"""Read-only Bybit Snapshot+Delta evidence, not trading authority.

The explicit mark timestamp never changes on an omitted mark field. A valid
same-topic ordered delta can separately confirm the maintained value. Bybit's
cross sequence is NOT documented as consecutive per topic, so this module
cannot prove detection of exchange-internal message loss from sequence jumps.
A local continuity break invalidates the chain until a new complete mark
snapshot. Nothing here is used by the production risk/price decoder.
"""
from dataclasses import dataclass
import math
import threading
import time
import uuid

from .ws_symbols import USDT_SYMBOL

# A symbol alone or an unknown-only payload is not ticker evidence.
TICKER_FIELDS = frozenset(('markPrice', 'indexPrice', 'lastPrice', 'tickDirection',
    'price24hPcnt', 'prevPrice24h', 'highPrice24h', 'lowPrice24h', 'prevPrice1h',
    'openInterest', 'openInterestValue', 'singleOpenInterest', 'singleOpenInterestValue',
    'turnover24h', 'volume24h', 'nextFundingTime', 'fundingRate', 'bid1Price',
    'bid1Size', 'ask1Price', 'ask1Size', 'deliveryTime', 'basisRate', 'deliveryFeeRate',
    'predictedDeliveryPrice', 'basis', 'preOpenPrice', 'preQty', 'curPreListingPhase',
    'fundingIntervalHour', 'fundingCap', 'basisRateYear'))


@dataclass(frozen=True)
class MarkState:
    price: float
    explicit_ms: int
    confirmed_ms: int
    sequence: int
    received_mono: float
    generation: str
    chain: int = 0


class TickerState:
    """One owned subscription generation, with bounded per-symbol state."""

    def __init__(self, symbols, *, age_bounds, monotonic=time.monotonic):
        names = tuple(symbols)
        if not names or len(names) != len(set(names)) or any(
                type(s) is not str or not USDT_SYMBOL.fullmatch(s) for s in names):
            raise ValueError('invalid ticker state cohort')
        self.symbols = names
        self._symbols = frozenset(names)
        self._age = age_bounds
        self._mono = monotonic
        self._lock = threading.RLock()
        self._generation = None
        self._states = {}
        # Diagnostic explicit-field evidence is independent of a delta chain.
        self._explicit = {}
        self._explicit_watermark = {}
        self._last_valid_stamp = {}
        self._retired_stamp = {}
        self._first_breaks = {}
        self._current_frame = None
        self._chain_counter = 0
        self._rejections = {}
        self._counts = dict(snapshots=0, explicit_marks=0, omitted_mark_deltas=0,
                            rejected=0, continuity_breaks=0, duplicates=0,
                            cross_sequence_jumps=0, raw_explicit_marks=0,
                            raw_explicit_conflicts=0, raw_explicit_regressions=0)

    def connect(self):
        with self._lock:
            self._states.clear()
            self._explicit.clear()
            self._explicit_watermark.clear()
            self._last_valid_stamp.clear()
            self._retired_stamp.clear()
            self._generation = uuid.uuid4().hex
            return self._generation

    def disconnect(self, generation):
        with self._lock:
            if generation == self._generation:
                self._generation = None
                self._states.clear()
                self._explicit.clear()
                self._explicit_watermark.clear()
                self._last_valid_stamp.clear()
                self._retired_stamp.clear()

    def _invalidate(self, symbol, reason):
        counts = self._rejections.setdefault(symbol, {})
        counts[reason] = counts.get(reason, 0) + 1
        self._counts['rejected'] += 1
        prior = self._states.pop(symbol, None)
        if prior is not None:
            self._counts['continuity_breaks'] += 1
            self._first_breaks.setdefault(symbol, dict(reason=reason,
                previous_cs=prior.sequence, previous_ts=prior.confirmed_ms,
                previous_explicit_ms=prior.explicit_ms, chain=prior.chain,
                local_gap_seconds=round(self._mono()-prior.received_mono, 6),
                current=self._current_frame))
        return False

    def reset_topics(self, symbols, generation):
        """Retire only these diagnostic subscriptions before resnapshotting."""
        with self._lock:
            if generation != self._generation or self._generation is None:
                return False
            if not set(symbols) <= self._symbols:
                raise ValueError('unknown reset topic')
            self._current_frame = None
            for symbol in symbols:
                if symbol in self._states:
                    self._invalidate(symbol, 'diagnostic_subscription_retired')
                self._retired_stamp[symbol] = self._last_valid_stamp.get(symbol, 0)
                self._explicit.pop(symbol, None)
                self._explicit_watermark.pop(symbol, None)
            return True

    def _record_explicit(self, symbol, price, stamp, sequence, now, generation):
        """Measure original field arrival even if maintained chain is broken.

        A backwards timestamp or conflicting same-timestamp price cannot
        replace the last independently observed explicit evidence. This is
        source diagnostics, not authorization to trade from a broken chain.
        """
        prior = self._explicit_watermark.get(symbol)
        if prior is not None and stamp <= prior[0]:
            if stamp < prior[0]:
                self._counts['raw_explicit_regressions'] += 1
            elif price != prior[1]:
                self._counts['raw_explicit_conflicts'] += 1
                self._explicit.pop(symbol, None)
            return
        self._explicit_watermark[symbol] = (stamp, price)
        self._explicit[symbol] = MarkState(price, stamp, stamp, sequence, now, generation)
        self._counts['raw_explicit_marks'] += 1

    def _fresh(self, stamp):
        if type(stamp) is not int or stamp <= 0:
            return False
        try:
            lo, hi = self._age(stamp / 1000)
            return math.isfinite(lo) and math.isfinite(hi) and -.5 <= lo <= hi <= 3.
        except (ValueError, TypeError, OverflowError):
            return False

    def ingest(self, payload, generation):
        """Accept an ordered frame from the caller's authenticated socket.

        An omitted mark is usable only following a real mark snapshot and
        while the same-symbol stream has no observed break. A silent topic,
        invalid frame, backwards sequence, or excessive gap cannot confirm.
        Other symbols, ACKs and pongs never confirm a mark.
        """
        with self._lock:
            if self._generation is None or generation != self._generation:
                return False
            if type(payload) is not dict:
                self.disconnect(generation)
                return False
            topic = payload.get('topic')
            if type(topic) is not str or not topic.startswith('tickers.'):
                return False
            symbol = topic[len('tickers.'):]
            if symbol not in self._symbols:
                return False
            kind, data = payload.get('type'), payload.get('data')
            stamp, sequence = payload.get('ts'), payload.get('cs')
            # Bounded first-break metadata, not a retained message stream.
            self._current_frame = dict(
                cs=sequence if type(sequence) is int else None,
                ts=stamp if type(stamp) is int else None,
                cs_type=type(sequence).__name__, ts_type=type(stamp).__name__,
                kind=kind if kind in ('snapshot', 'delta') else None,
                symbol_matches=type(data) is dict and data.get('symbol') == symbol,
                fields=sorted(set(data) & TICKER_FIELDS) if type(data) is dict else [],
                explicit_mark=type(data) is dict and 'markPrice' in data)
            if (kind not in ('snapshot', 'delta') or type(data) is not dict
                    or data.get('symbol') != symbol or not TICKER_FIELDS.intersection(data)
                    or type(sequence) is not int or sequence < 0 or not self._fresh(stamp)):
                return self._invalidate(symbol, 'invalid_shape_sequence_or_age')
            explicit = 'markPrice' in data
            price = None
            if explicit:
                try:
                    if type(data['markPrice']) not in (str, int, float):
                        raise ValueError('invalid mark type')
                    price = float(data['markPrice'])
                    if not math.isfinite(price) or price <= 0:
                        raise ValueError('invalid mark price')
                except (TypeError, ValueError, OverflowError):
                    return self._invalidate(symbol, 'invalid_mark')
            # Refuse malformed nested fields, booleans, or invalid numeric
            # content in executable price fields; optional strings may be empty.
            if any(type(v) not in (str, int, float) or
                   (type(v) is float and not math.isfinite(v)) for v in data.values()):
                return self._invalidate(symbol, 'invalid_field_type')
            for field in ('lastPrice', 'bid1Price', 'ask1Price', 'indexPrice'):
                if field in data and data[field] != '':
                    try:
                        p = float(data[field])
                        if not math.isfinite(p) or p < 0:
                            raise ValueError('invalid ticker price')
                    except (TypeError, ValueError, OverflowError):
                        return self._invalidate(symbol, 'invalid_price')
            if stamp <= self._retired_stamp.get(symbol, 0):
                return self._invalidate(symbol, 'retired_subscription_timestamp')
            self._last_valid_stamp[symbol] = max(stamp, self._last_valid_stamp.get(symbol, 0))
            prior = self._states.get(symbol)
            now = self._mono()
            if explicit:
                self._record_explicit(symbol, price, stamp, sequence, now, generation)
            if prior is not None:
                if sequence == prior.sequence and stamp == prior.confirmed_ms:
                    if explicit and price != prior.price:
                        return self._invalidate(symbol, 'conflicting_duplicate')
                    self._counts['duplicates'] += 1
                    return False
                if stamp < prior.confirmed_ms:
                    return self._invalidate(symbol, 'timestamp_regression')
                if sequence < prior.sequence:
                    return self._invalidate(symbol, 'sequence_regression')
                if sequence == prior.sequence:
                    return self._invalidate(symbol, 'equal_sequence_new_timestamp')
            if kind == 'snapshot':
                if not explicit:
                    return self._invalidate(symbol, 'snapshot_without_mark')
                self._counts['snapshots'] += 1
                self._chain_counter += 1
                chain = self._chain_counter
            elif prior is None:
                return self._invalidate(symbol, 'missing_snapshot')
            elif not 0 <= now-prior.received_mono <= 3.:
                return self._invalidate(symbol, 'local_receive_gap')
            elif stamp-prior.confirmed_ms > 3000:
                return self._invalidate(symbol, 'exchange_timestamp_gap')
            else:
                chain = prior.chain
            if prior is not None and sequence > prior.sequence + 1:
                self._counts['cross_sequence_jumps'] += 1
            if explicit:
                self._counts['explicit_marks'] += 1
                explicit_ms = stamp
            else:
                self._counts['omitted_mark_deltas'] += 1
                explicit_ms, price = prior.explicit_ms, prior.price
            self._states[symbol] = MarkState(price, explicit_ms, stamp, sequence, now, generation, chain)
            return True

    def _fresh_observations(self, states, now):
        result = {}
        for symbol in self.symbols:
            state = states.get(symbol)
            result[symbol] = (state if state is not None and self._generation is not None
                and state.generation == self._generation
                and 0 <= now-state.received_mono <= 3.
                and self._fresh(state.confirmed_ms) else None)
        return result

    def observations(self):
        """Maintained-state evidence only; never resurrect a broken chain."""
        with self._lock:
            return self._fresh_observations(self._states, self._mono())

    def comparison_observations(self):
        """Two independent metrics, coherently read under the same lock."""
        with self._lock:
            now = self._mono()
            return (self._fresh_observations(self._explicit, now),
                    self._fresh_observations(self._states, now))

    def stats(self):
        with self._lock:
            return dict(self._counts, initialized_symbols=len(self._states),
                        connected=self._generation is not None,
                        rejections_by_symbol={s:dict(c) for s,c in self._rejections.items()},
                        first_breaks={s:dict(c) for s,c in self._first_breaks.items()})
