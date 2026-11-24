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
        self._chain_counter = 0
        self._rejections = {}
        self._counts = dict(snapshots=0, explicit_marks=0, omitted_mark_deltas=0,
                            rejected=0, continuity_breaks=0, duplicates=0,
                            cross_sequence_jumps=0)

    def connect(self):
        with self._lock:
            self._states.clear()
            self._generation = uuid.uuid4().hex
            return self._generation

    def disconnect(self, generation):
        with self._lock:
            if generation == self._generation:
                self._generation = None
                self._states.clear()

    def _invalidate(self, symbol, reason):
        counts = self._rejections.setdefault(symbol, {})
        counts[reason] = counts.get(reason, 0) + 1
        self._counts['rejected'] += 1
        if self._states.pop(symbol, None) is not None:
            self._counts['continuity_breaks'] += 1
        return False

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
            prior = self._states.get(symbol)
            now = self._mono()
            if prior is not None:
                if sequence == prior.sequence and stamp == prior.confirmed_ms:
                    if explicit and price != prior.price:
                        return self._invalidate(symbol, 'conflicting_duplicate')
                    self._counts['duplicates'] += 1
                    return False
                if sequence <= prior.sequence or stamp < prior.confirmed_ms:
                    return self._invalidate(symbol, 'sequence_or_timestamp_regression')
            if kind == 'snapshot':
                if not explicit:
                    return self._invalidate(symbol, 'snapshot_without_mark')
                self._counts['snapshots'] += 1
                self._chain_counter += 1
                chain = self._chain_counter
            elif (prior is None or not 0 <= now-prior.received_mono <= 3.
                  or stamp-prior.confirmed_ms > 3000):
                return self._invalidate(symbol, 'missing_snapshot_or_continuity_gap')
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

    def observations(self):
        """Coherent same-process read; retain both timestamps for comparison."""
        with self._lock:
            now = self._mono()
            result = {}
            for symbol in self.symbols:
                state = self._states.get(symbol)
                if (state is None or self._generation is None
                        or state.generation != self._generation
                        or not 0 <= now-state.received_mono <= 3.
                        or not self._fresh(state.confirmed_ms)):
                    result[symbol] = None
                else:
                    result[symbol] = state
            return result

    def stats(self):
        with self._lock:
            return dict(self._counts, initialized_symbols=len(self._states),
                        connected=self._generation is not None,
                        rejections_by_symbol={s:dict(c) for s,c in self._rejections.items()})
