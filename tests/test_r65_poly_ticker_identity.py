"""Wrong-asset public price must never drive Polymarket UP/DOWN PAPER entry."""

import pytest

from bcc.market import DataUnavailable, PolymarketData


def test_impulse_rejects_ticker_of_different_coin(monkeypatch):
    feed = PolymarketData()
    start = 1760000100
    def forged_response(url, params=None, **kwargs):
        if url.endswith("/klines"):
            return [[start * 1000, "100", "101", "99", "100", "123"]]
        if url.endswith("/ticker/price"):
            return {"symbol": "ETHUSDT", "price": "110"}
        raise AssertionError(url)
    monkeypatch.setattr(feed, "get", forged_response)
    with pytest.raises(DataUnavailable, match="другой|пары|символ"):
        feed.impulse("BTC", start, start + 40)


def test_impulse_accepts_matching_public_ticker(monkeypatch):
    feed = PolymarketData()
    start = 1760000100
    def response(url, params=None, **kwargs):
        if url.endswith("/klines"):
            return [[start * 1000, "100", "101", "99", "100", "123"]]
        if url.endswith("/ticker/price"):
            return {"symbol": "BTCUSDT", "price": "110"}
        raise AssertionError(url)
    monkeypatch.setattr(feed, "get", response)
    assert feed.impulse("BTC", start, start + 40) == (10, pytest.approx(10))
