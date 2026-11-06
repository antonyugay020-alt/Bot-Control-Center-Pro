"""Bounded Modern Qt Polymarket market enumeration and selector contract.

A bot may configure many coins and candle durations.  The preview is bounded,
while editing must reach every configured market without building a giant
cartesian-product dialog.  This module has no Qt, repository or worker imports.
"""

from itertools import islice, product


def market_axes(config):
    """Return configured coin/duration axes without constructing all markets."""
    if not isinstance(config, dict) or config.get("strategy") != "POLYMARKET":
        raise ValueError("Выберите конструктор Polymarket")
    symbols = config.get("symbols")
    poly = config.get("polymarket")
    durations = poly.get("durations") if isinstance(poly, dict) else None
    if (not isinstance(symbols, list) or not isinstance(durations, list)
            or not symbols or not durations
            or any(not isinstance(symbol, str) or not symbol for symbol in symbols)
            or any(type(duration) is not int or duration <= 0 for duration in durations)):
        raise ValueError("Неверные монеты или длительности рынков")
    return symbols, durations


def market_preview(config, limit=300):
    """Return bounded rows and full count; never materialize all combinations."""
    if type(limit) is not int or limit < 0:
        raise ValueError("Неверный размер предпросмотра")
    symbols, durations = market_axes(config)
    return list(islice(product(symbols, durations), limit)), len(symbols) * len(durations)


def market_key(config, symbol, duration):
    """Validate a selection from either the preview or the two-axis picker."""
    symbols, durations = market_axes(config)
    if symbol not in symbols or type(duration) is not int or duration not in durations:
        raise ValueError("Выбранный рынок отсутствует в конструкторе")
    return f"{symbol}:{duration}"
