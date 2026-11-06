"""A 403/451 must fence the entire fleet, not only the rejecting worker."""

import json
import time

import pytest

from bcc.data_cache import CacheBusy, PublicCache
from bcc.market import PublicData, SourceAccessDenied


HOST = "api.bybit.com"
URL = "https://api.bybit.com/v5/market/tickers"
PARAMS = {"category": "linear", "symbol": "BTCUSDT"}


@pytest.mark.parametrize("status", [403, 451])
def test_provider_denial_is_durable_across_independent_cache_clients(tmp_path, status):
    path = tmp_path / "fleet_public.sqlite"
    producer = PublicCache(path)
    consumer = PublicCache(path)
    try:
        key, owner, cached = producer.begin(URL, PARAMS, HOST)
        assert owner and cached is None
        producer.finish(key, owner, HOST, cooldown=3600, denial_status=status)
        assert consumer.denied_status(HOST) == status
        with pytest.raises(CacheBusy, match="отклонил публичный доступ"):
            consumer.read_fresh(URL, PARAMS, HOST)
        with pytest.raises(CacheBusy, match="отклонил публичный доступ"):
            consumer.begin(URL, PARAMS, HOST)
        # Other independent providers must remain accessible.
        assert consumer.denied_status("gamma-api.polymarket.com") is None
    finally:
        producer.close()
        consumer.close()


@pytest.mark.parametrize("hub_client", [False, True])
def test_second_worker_rejects_preexisting_memory_cache_after_denial(
    tmp_path, monkeypatch, hub_client
):
    path = tmp_path / "fleet_public.sqlite"
    producer = PublicCache(path)
    transport = PublicData()
    transport.shared = PublicCache(path)
    transport.cache[(URL, json.dumps(PARAMS, sort_keys=True))] = (
        time.time(), {"retCode": 0, "result": {"list": []}}
    )
    if hub_client:
        monkeypatch.setenv("BCC_MARKET_HUB_MODE", "client")
    else:
        monkeypatch.delenv("BCC_MARKET_HUB_MODE", raising=False)
    try:
        key, owner, cached = producer.begin(URL, PARAMS, HOST)
        assert cached is None and owner
        producer.finish(key, owner, HOST, cooldown=3600, denial_status=451)
        with pytest.raises(SourceAccessDenied) as exc:
            transport.get(URL, PARAMS, ttl=3600)
        assert (exc.value.host, exc.value.http_status) == (HOST, 451)
    finally:
        transport.close()
        producer.close()


def test_denial_must_not_be_written_by_obsolete_lease_owner(tmp_path):
    path = tmp_path / "fleet_public.sqlite"
    cache = PublicCache(path)
    try:
        key, owner, _ = cache.begin(URL, PARAMS, HOST)
        cache.finish(key, "other-worker", HOST, cooldown=3600, denial_status=451)
        assert cache.denied_status(HOST) is None
        cache.finish(key, owner, HOST, cooldown=3600, denial_status=451)
        assert cache.denied_status(HOST) == 451
    finally:
        cache.close()


@pytest.mark.parametrize("status", [403, 451])
def test_real_http_policy_refusal_is_published_to_other_worker(tmp_path, monkeypatch, status):
    monkeypatch.delenv("BCC_MARKET_HUB_MODE", raising=False)
    path = tmp_path / "fleet_public.sqlite"
    transport = PublicData()
    transport.shared = PublicCache(path)

    class FakeResponse:
        status_code = status
        headers = {"Retry-After": "120"}

    class FakeSession:
        def get(self, *args, **kwargs):
            return FakeResponse()

        def close(self):
            pass

    transport._thread.session = FakeSession()
    reader = PublicCache(path)
    try:
        with pytest.raises(SourceAccessDenied):
            transport.get(URL, PARAMS)
        assert reader.denied_status(HOST) == status
        with pytest.raises(CacheBusy, match="отклонил публичный доступ"):
            reader.begin(URL, PARAMS, HOST)
    finally:
        transport.close()
        reader.close()
