"""Official bulk REST collateral snapshot; never an executable quote.

The API time describes the response snapshot, not a per-symbol WS event.
Preserve it across publication/read, and never renew it from local receipt.
"""
import hashlib
import json
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from .ws_symbols import USDT_SYMBOL

URL = 'https://api.bybit.com/v5/market/tickers'
HOST = 'api.bybit.com'
KEY = hashlib.sha256(b'bcc.bybit.rest.mark.snapshot.v1').hexdigest()
KIND = 'bybit_rest_linear_snapshot_v1'


def enabled():
    return os.environ.get('BCC_BYBIT_REST_MARK_FALLBACK') == '1'


def client_enabled():
    return (enabled() and os.environ.get('BCC_MARKET_HUB_MODE') == 'client'
            and os.environ.get('BCC_BYBIT_WS_QUOTES') == '1')


def _price(value):
    if type(value) not in (str, int, float):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def _stamp(value, fresh):
    return (type(value) is int and 0 < value < 2**63
            and fresh(value / 1000))


def build_snapshot(payload, fresh):
    """Accept only an actual successful linear bulk response. No WS metadata."""
    if (type(payload) is not dict or type(payload.get('retCode')) is not int
            or payload['retCode'] != 0 or any(k.startswith('_bcc_') for k in payload)
            or not _stamp(payload.get('time'), fresh)):
        return None
    result = payload.get('result')
    if type(result) is not dict or result.get('category') != 'linear':
        return None
    rows = result.get('list')
    if type(rows) is not list or not 0 < len(rows) <= 10000:
        return None
    seen, marks = set(), {}
    for row in rows:
        if type(row) is not dict:
            continue
        symbol = row.get('symbol')
        if type(symbol) is not str or not USDT_SYMBOL.fullmatch(symbol):
            continue
        if symbol in seen:
            marks.pop(symbol, None)
            continue
        seen.add(symbol)
        price = _price(row.get('markPrice'))
        if price is not None and not any(k.startswith('_bcc_') for k in row):
            marks[symbol] = price
    return dict(kind=KIND, time=payload['time'], marks=marks) if marks else None


def index_snapshot(value, fresh):
    if (type(value) is not dict or set(value) != {'kind','time','marks'}
            or value['kind'] != KIND or not _stamp(value['time'], fresh)
            or type(value['marks']) is not dict or len(value['marks']) > 10000):
        return {}
    at = value['time'] / 1000
    return {symbol:dict(bid=price, ask=price, at=at, mark_basis='rest_snapshot')
            for symbol, raw in value['marks'].items()
            if type(symbol) is str and USDT_SYMBOL.fullmatch(symbol)
            and (price := _price(raw)) is not None}


def publish_compact(db, compact, encoded, fresh):
    """Inside the caller's admitted writer transaction; only the mark key.

    An ordinary bulk ticker response has a separate cache contract. Do not
    overwrite that raw row or churn the WAL with its unused order-book fields.
    The caller owns the provider, lease, budget and Hub-generation fences.
    """
    old = db.execute("SELECT value FROM cache WHERE k=? AND host=?", (KEY, HOST)).fetchone()
    old_stamp = json.loads(old[0]).get("time", 0) if old else 0
    if (not fresh(compact['time'] / 1000)
            or (type(old_stamp) is int and compact['time'] <= old_stamp)):
        return False
    db.execute("INSERT OR REPLACE INTO cache(k,value,until,host) VALUES(?,?,?,?)",
               (KEY, encoded, time.time() + 3., HOST))
    return True


class RestMarkPump:
    """One background job, no bursts or overlap, no work without admission."""
    def __init__(self, fetch, *, recoverable=(), monotonic=time.monotonic, period=1.):
        if type(period) not in (float, int) or not .5 <= period <= 1.:
            raise ValueError('Invalid bulk mark polling period')
        self.period = float(period)
        self.fetch = fetch
        self.recoverable = recoverable
        self.monotonic = monotonic
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='rest-mark')
        self.job = None
        self.next_due = 0.
        self.started = self.skipped = self.completed = self.errors = 0
        self.last_started = None
        self.max_start_gap = 0.
        self.error_types = {}

    def poll(self, ready):
        if self.job is not None and self.job.done():
            job, self.job = self.job, None
            try:
                outcome = job.result()
            except self.recoverable as exc:
                self.errors += 1
                # Classify only, never log exchange payloads or request secrets.
                name = type(exc.__cause__ or exc).__name__
                self.error_types[name] = self.error_types.get(name, 0) + 1
            else:
                if outcome:
                    self.completed += 1
                else:
                    self.skipped += 1
        now = self.monotonic()
        if ready and self.job is None and now >= self.next_due:
            self.next_due = now + self.period
            if self.last_started is not None:
                self.max_start_gap = max(self.max_start_gap, now - self.last_started)
            self.last_started = now
            self.started += 1
            self.job = self.pool.submit(self.fetch)

    def stats(self):
        return dict(started=self.started, skipped=self.skipped, completed=self.completed,
                    errors=self.errors, pending=self.job is not None,
                    period_seconds=self.period, max_start_gap_seconds=round(self.max_start_gap, 6),
                    error_types=dict(self.error_types))

    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=True)
