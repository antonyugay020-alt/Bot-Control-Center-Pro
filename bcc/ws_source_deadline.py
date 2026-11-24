"""Original exchange timestamp retention for the opt-in Bybit WS producer.

All fields in one cached envelope expire at their earliest existing deadline.
This policy cannot create prices, extend exchange timestamps, grant provider
access, or join different connection incarnations.
"""
import math

from .market_clock import book_age_bounds


def remaining_bybit_ws_seconds(value):
    """Conservative remaining lifetime, or zero for invalid/stale evidence.

    A mark or book may live at most 3 seconds from its source timestamp;
    executable ticker sides retain the existing stricter 2-second limit.
    No fallback time or network request is performed here.
    """
    try:
        if type(value) is not dict or value.get('retCode') != 0:
            return 0.0
        mark = value.get('_bcc_ws_mark') is True
        quote = value.get('_bcc_ws_quote') is True
        book = value.get('_bcc_ws_book') is True
        if book:
            if mark or quote:
                return 0.0
            stamp = value['result']['ts']
            if stamp != value['time']:
                return 0.0
            fields = [(stamp, 3.0)]
        else:
            fence = value.get('_bcc_ws_connection_fence')
            if not (mark or quote) or type(fence) is not str or len(fence) != 32:
                return 0.0
            fields = []
            if mark:
                fields.append((value['_bcc_ws_mark_at'], 3.0))
            if quote:
                fields.append((value['time'], 2.0))
        remaining = []
        for stamp, limit in fields:
            if type(stamp) is not int or stamp <= 0:
                return 0.0
            low, high = book_age_bounds(stamp/1000)
            if not (math.isfinite(low) and math.isfinite(high)
                    and -.5 <= low <= high < limit):
                return 0.0
            remaining.append(min(limit,limit-high))
        return min(remaining)
    except (KeyError, ValueError, TypeError, OverflowError):
        return 0.0
