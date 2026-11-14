"""Fast WS position lane must not block behind slow Bybit risk scans."""
import pytest

from bcc.market import PublicData


def _instrument(symbol):
    return {
        "retCode": 0,
        "result": {"list": [{
            "symbol": symbol, "status": "Trading",
            "contractType": "LinearPerpetual", "quoteCoin": "USDT",
            "lotSizeFilter": {
                "qtyStep": "0.001", "minOrderQty": "0.001",
                "minNotionalValue": "5", "maxMktOrderQty": "100"
            },
            "priceFilter": {"tickSize": "0.01"}
        }]}
    }


def _book(symbol):
    return {
        "retCode": 0, "time": 1000000,
        "result": {
            "s": symbol, "ts": 1000000,
            "b": [["99.98", "10"], ["99.97", "20"]],
            "a": [["100.02", "10"], ["100.03", "20"]],
            "u": 3, "seq": 4,
        },
    }


def test_existing_position_uses_executable_book_without_full_fleet_risk_scan(monkeypatch):
    symbol = "BTCUSDT"
    source = PublicData()
    requests, risk_calls = [], []
    monkeypatch.setattr("bcc.market.clock.ensure", lambda: None)
    monkeypatch.setattr("bcc.market.market_now", lambda: 1000.0)
    monkeypatch.setattr("bcc.market.check_book_time", lambda *a, **kw: None)

    def get(url, params=None, ttl=0):
        requests.append((url, params))
        if url.endswith("/instruments-info"):
            return _instrument(symbol)
        if url.endswith("/orderbook"):
            return _book(symbol)
        raise AssertionError("Unexpected GET " + url)

    def margin(symbol):
        risk_calls.append(symbol)
        return {"symbol": symbol, "tiers": [], "account_marks": {}}

    monkeypatch.setattr(source, "get", get)
    monkeypatch.setattr(source, "margin_snapshot", margin)
    try:
        position = source.position_orderbook(symbol)
        assert position["at"] == 1000.0
        assert position["margin_data"] is None
        assert not risk_calls
        assert [req[0].rsplit("/", 1)[-1] for req in requests] == [
            "instruments-info", "orderbook"
        ]
        scanner = source.orderbook(symbol)
        assert scanner["at"] == position["at"]
        assert risk_calls == [symbol]
        assert scanner["margin_required"] is True
    finally:
        source.close()


def test_position_only_intent_must_be_boolean():
    source = PublicData()
    try:
        with pytest.raises(ValueError, match="position"):
            source.orderbook("BTCUSDT", position_only="yes")
    finally:
        source.close()
