"""The Polymarket selection catalog cannot depend on an arbitrary Gamma page."""

from bcc.market import COIN_NAMES, PolymarketData, DataUnavailable
from bcc.config import defaults, validate
import pytest


def test_catalog_is_stable_even_when_gamma_is_unreachable(monkeypatch):
    source = PolymarketData()
    monkeypatch.setattr(source, "get", lambda *a, **k: (_ for _ in ()).throw(DataUnavailable("Gamma offline")))
    try:
        assert source.list_symbols() == sorted(COIN_NAMES)
        assert {"BTC", "ETH", "SOL", "XRP", "DOGE", "HYPE", "ZEC"} <= set(source.list_symbols())
    finally:
        source.close()


def test_supported_symbol_does_not_bypass_market_validation(monkeypatch):
    source = PolymarketData()
    monkeypatch.setattr("bcc.market.clock.ensure", lambda: None)
    monkeypatch.setattr("bcc.market.market_now", lambda now=None: 1800000001.0)
    monkeypatch.setattr(source, "get", lambda *a, **kw: [])
    try:
        assert "BTC" in source.list_symbols()
        with pytest.raises(DataUnavailable, match="Не найден единственный активный"):
            source.discover("BTC", 300, 1800000001.0)
    finally:
        source.close()


def test_supported_catalog_preserves_existing_polymarket_configuration():
    cfg = defaults("POLYMARKET")
    cfg["name"] = "Catalog boundary"
    old = validate(cfg)
    feed = PolymarketData()
    try:
        assert feed.list_symbols()
        assert validate(cfg) == old
    finally:
        feed.close()
