"""Independent Bybit ticker evidence in a single atomic cache record.

Mark and executable quote have separate origin fences and original clocks.
Physical row residence never authorizes either field: every reader projects
only independently valid observations, without carrying a prior socket's data
into a new publication. The REST-shaped projection is a compatibility view.
"""
import math

from .market_clock import book_age_bounds
from .ws_symbols import USDT_SYMBOL
from .ws_maintained_marks import maintained_metadata


def valid_fence(value):
    return (type(value) is str and len(value) == 32
            and all(c in '0123456789abcdef' for c in value))


def remaining(stamp, limit):
    if type(stamp) is not int or stamp <= 0:
        return 0.
    try:
        low, high = book_age_bounds(stamp / 1000)
        if not (math.isfinite(low) and math.isfinite(high)
                and -.5 <= low <= high < limit):
            return 0.
        return min(limit, limit - high)
    except (TypeError, ValueError, OverflowError):
        return 0.


def make_split_ticker(symbol, mark=None, quote=None):
    """Each argument belongs to exactly one independently authenticated lane.

    mark=(price, original_seconds, fence), quote=(bid, ask, original_seconds,
    fence). The legacy fence is exposed only if the fields share an origin.
    No stale row from SQLite is an input to this function.
    """
    row = {'symbol': symbol}
    mark_at = quote_at = mark_fence = quote_fence = None
    if mark is not None:
        price, at, mark_fence = mark[:3]
        row['markPrice'] = str(price)
        mark_at = round(at * 1000)
    if quote is not None:
        bid, ask, at, quote_fence = quote
        row.update(bid1Price=str(bid), ask1Price=str(ask))
        quote_at = round(at * 1000)
    value = dict(retCode=0, retMsg='OK', _bcc_ws_split_ticker=True,
                _bcc_ws_source_deadline=True, _bcc_ws_mark=mark is not None,
                _bcc_ws_quote=quote is not None, _bcc_ws_mark_at=mark_at,
                _bcc_ws_mark_fence=mark_fence, _bcc_ws_quote_fence=quote_fence,
                _bcc_ws_connection_fence=(mark_fence or quote_fence)
                if not (mark_fence and quote_fence and mark_fence != quote_fence) else None,
                time=quote_at if quote_at is not None else mark_at,
                result={'list': [row]})
    if mark is not None and len(mark) == 6:
        value.update(_bcc_ws_mark_basis='snapshot_delta',
                     _bcc_ws_mark_explicit_at=mark[3], _bcc_ws_mark_cs=mark[4],
                     _bcc_ws_mark_chain=mark[5])
        maintained_metadata(value)
    return value


def empty_split_ticker(value, symbol):
    """A leader-owned revocation envelope, never a readable price."""
    return type(value) is dict and value == make_split_ticker(symbol)


def project_ticker(value, *, symbol=None):
    """Return (fresh field-only view, longest remaining physical residence).

    The view never contains expired fields. Legacy candidate input may have
    a single source fence; the split format never falls back to that fence.
    Invalid or absent observations fail independently. Unknown fields are not
    carried into a verified market-data view.
    """
    try:
        if type(value) is not dict or value.get('retCode') != 0:
            return None, 0.
        if value.get('_bcc_ws_book') is True:
            return None, 0.
        rows = value['result']['list']
        if type(rows) is not list or len(rows) != 1 or type(rows[0]) is not dict:
            return None, 0.
        row = rows[0]
        name = row['symbol']
        if type(name) is not str or not USDT_SYMBOL.fullmatch(name) or (symbol is not None and name != symbol):
            return None, 0.
    except (KeyError, TypeError, ValueError):
        return None, 0.
    split = value.get('_bcc_ws_split_ticker') is True
    legacy = value.get('_bcc_ws_connection_fence')
    mark_fence = value.get('_bcc_ws_mark_fence') if split else legacy
    quote_fence = value.get('_bcc_ws_quote_fence') if split else legacy
    mark = quote = None
    mark_ttl = quote_ttl = 0.
    if value.get('_bcc_ws_mark') is True and valid_fence(mark_fence):
        try:
            stamp = value['_bcc_ws_mark_at']
            price = float(row['markPrice'])
            if not isinstance(row['markPrice'], bool) and math.isfinite(price) and price > 0:
                mark_ttl = remaining(stamp, 3.)
                if mark_ttl:
                    meta = maintained_metadata(value)
                    mark = (price, stamp / 1000, mark_fence)
                    if meta is not None:
                        mark += meta
        except (KeyError, TypeError, ValueError, OverflowError):
            pass
    if value.get('_bcc_ws_quote') is True and valid_fence(quote_fence):
        try:
            stamp = value['time']
            bid, ask = float(row['bid1Price']), float(row['ask1Price'])
            if (not isinstance(row['bid1Price'], bool) and not isinstance(row['ask1Price'], bool)
                    and math.isfinite(bid) and math.isfinite(ask) and 0 < bid <= ask):
                quote_ttl = remaining(stamp, 2.)
                if quote_ttl:
                    quote = (bid, ask, stamp / 1000, quote_fence)
        except (KeyError, TypeError, ValueError, OverflowError):
            pass
    if mark is None and quote is None:
        return None, 0.
    projected = make_split_ticker(name, mark, quote)
    # Preserve original integer exchange timestamps bit-for-bit, including
    # millisecond values that cannot round-trip exactly through a float.
    if mark is not None:
        projected['_bcc_ws_mark_at'] = value['_bcc_ws_mark_at']
    projected['time'] = value['time'] if quote is not None else value['_bcc_ws_mark_at']
    return projected, max(mark_ttl, quote_ttl)
