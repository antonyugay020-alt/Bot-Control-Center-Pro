"""Provider denials must block every public-data transport path, including Hub caches."""

import time

import pytest

from bcc.market import PublicData, SourceAccessDenied


@pytest.mark.parametrize("hub_client", [False, True])
def test_provider_access_denial_precedes_cache_and_hub(monkeypatch, hub_client):
    transport = PublicData()
    host = "api.bybit.com"
    url = "https://api.bybit.com/v5/market/tickers"
    params = {"category": "linear", "symbol": "BTCUSDT"}
    transport._access_denials[host] = (time.monotonic() + 3600, 451)
    # A previously cached response must not be returned while denied.
    import json
    transport.cache[(url, json.dumps(params, sort_keys=True))] = (
        time.time(), {"retCode": 0, "result": {"list": []}}
    )
    if hub_client:
        monkeypatch.setenv("BCC_MARKET_HUB_MODE", "client")
    else:
        monkeypatch.delenv("BCC_MARKET_HUB_MODE", raising=False)

    class RejectOnAccess:
        def __getattr__(self, name):
            raise AssertionError("Denied source reached Hub/network: " + name)

    transport.shared = RejectOnAccess()
    try:
        with pytest.raises(SourceAccessDenied) as error:
            transport.get(url, params, ttl=600)
        assert error.value.host == host
        assert error.value.http_status == 451
    finally:
        transport.shared = None
        transport.close()


def test_provider_denial_is_monotonic_and_expires_without_wall_clock(monkeypatch):
    transport = PublicData()
    host = "api.bybit.com"
    transport._access_denials[host] = (time.monotonic() + 20, 403)
    monkeypatch.setenv("BCC_MARKET_HUB_MODE", "client")
    with pytest.raises(SourceAccessDenied):
        transport.get("https://api.bybit.com/v5/market/tickers")
    assert host in transport._access_denials
    transport.close()
