"""Immutable finalized Bybit minute evidence, separate from forming candles.

Only the Hub may persist evidence under its existing leader, subscription
and provider-policy transaction. These keys are NOT HTTP request identities.
The consumer still checks the exact required closed boundary and continuity.
"""
import hashlib
import json
import math

FIELDS = ('open', 'high', 'low', 'close', 'volume')
HOST = 'api.bybit.com'
RETENTION_SECONDS = 180


def key(symbol, start):
    from .ws_symbols import USDT_SYMBOL
    if (type(symbol) is not str or not USDT_SYMBOL.fullmatch(symbol)
            or type(start) is not int or start < 0 or start % 60000):
        raise ValueError('Invalid finalized minute identity')
    return hashlib.sha256(f'bcc.finalized.minute.v1:{symbol}:{start}'.encode()).hexdigest()


def validate(row, symbol, now_ms):
    if (type(row) is not dict or row.get('symbol') != symbol
            or type(row.get('t')) is not int or type(row.get('confirmed_ms')) is not int):
        raise ValueError('Invalid finalized minute evidence')
    start, stamp = row['t'], row['confirmed_ms']
    key(symbol, start)
    if (not start + 59999 <= stamp <= now_ms + 5000
            or not 0 <= now_ms - (start + 60000) < RETENTION_SECONDS * 1000):
        raise ValueError('Finalized minute outside current history window')
    values = [row.get(field) for field in FIELDS]
    if (any(type(v) not in (int, float) or not math.isfinite(v) for v in values)
            or min(values[:4]) <= 0 or values[4] < 0
            or values[2] > min(values[0], values[3])
            or values[1] < max(values[0], values[3])):
        raise ValueError('Invalid finalized minute OHLCV')
    return dict(symbol=symbol, t=start, confirmed_ms=stamp, **dict(zip(FIELDS, values)))


def remember(buffer, candle):
    """Keep genuine confirmation even if the forming minute has advanced."""
    if candle.confirmed is not True or candle.message_ms < candle.end_ms:
        return False
    row = dict(symbol=candle.symbol, t=candle.start_ms, confirmed_ms=candle.message_ms,
               **{field: getattr(candle, field) for field in FIELDS})
    bucket = buffer.setdefault(candle.symbol, {})
    prior = bucket.get(candle.start_ms)
    if prior is not None and (prior['confirmed_ms'] >= candle.message_ms
                              or all(prior[f] == row[f] for f in FIELDS)):
        return False
    bucket[candle.start_ms] = row
    for start in sorted(bucket)[:-3]:
        del bucket[start]
    return True


def snapshot(buffer, symbol, now_ms):
    rows = []
    for row in buffer.get(symbol, {}).values():
        try:
            rows.append(validate(row, symbol, now_ms))
        except ValueError:
            continue
    return rows


def prepare(rows, symbol, now_ms):
    if type(rows) is not list or len(rows) > 3:
        raise ValueError('Invalid finalized minute batch')
    result = []
    for row in rows:
        row = validate(row, symbol, now_ms)
        result.append((key(symbol, row['t']), json.dumps(row, sort_keys=True, allow_nan=False)))
    return result


def publish(db, records, now):
    """Caller owns the atomic admission transaction; never extend residence."""
    changed = 0
    for identity, encoded in records:
        changed += db.execute(
            'INSERT INTO cache(k,value,until,host) VALUES(?,?,?,?) '
            'ON CONFLICT(k) DO UPDATE SET value=excluded.value '
            'WHERE cache.host=excluded.host '
            'AND json_extract(cache.value,\'$.confirmed_ms\') < '
            'json_extract(excluded.value,\'$.confirmed_ms\')',
            (identity, encoded, now + RETENTION_SECONDS, HOST),
        ).rowcount
    return changed


def read(db, symbol, starts, now, now_ms):
    if not isinstance(starts, (list, tuple)) or not 1 <= len(starts) <= 3:
        raise ValueError('Invalid finalized minute read range')
    ids = {key(symbol, start): start for start in starts}
    found = db.execute(
        'SELECT k,value FROM cache WHERE host=? AND until>? AND k IN ('
        + ','.join('?' for _ in ids) + ')', (HOST, now, *ids),
    ).fetchall()
    rows = []
    for identity, encoded in found:
        try:
            row = validate(json.loads(encoded), symbol, now_ms)
            if row['t'] == ids[identity]:
                rows.append(row)
        except (ValueError, TypeError, OverflowError):
            continue
    return sorted(rows, key=lambda row: row['t'])
