"""Observed provider denials outlive HTTP singleflight leases (offline PAPER).

The provider's real HTTP refusal is a host-wide policy fact, not an
acknowledgement by the request-lease owner. A slow 403/451 must not disappear
when another worker takes over the request key. No live exchange calls.
"""
import json
import time

import pytest

from bcc.data_cache import ProviderDenied, PublicCache
from bcc.market import PublicData, SourceAccessDenied


HOST = "api.bybit.com"
URL = "https://api.bybit.com/v5/market/tickers"
PARAMS = {"category": "linear", "symbol": "BTCUSDT"}
OTHER_HOST = "gamma-api.polymarket.com"
OTHER_URL = "https://gamma-api.polymarket.com/markets"


@pytest.mark.parametrize("status", [403, 451])
@pytest.mark.parametrize("successor", [False, True])
def test_real_http_denial_is_durable_after_request_lease_lost(
    tmp_path, monkeypatch, status, successor
):
    monkeypatch.delenv("BCC_MARKET_HUB_MODE", raising=False)
    path = tmp_path / "public.sqlite"
    transport = PublicData()
    transport.shared = PublicCache(path)
    observer = PublicCache(path)
    old_owner = []

    class Response:
        status_code = status
        headers = {"Retry-After": "120"}

    class Session:
        def get(self, url, params, timeout, *, allow_redirects):
            assert allow_redirects is False
            with observer.db() as db:
                db.execute("BEGIN IMMEDIATE")
                key, owner = db.execute(
                    "SELECT k,owner FROM lease WHERE k IN "
                    "(SELECT k FROM lease) LIMIT 1"
                ).fetchone()
                old_owner.append(owner)
                if successor:
                    db.execute(
                        "UPDATE lease SET owner=?,until=? WHERE k=?",
                        ("new-worker", time.time() + 20, key),
                    )
                else:
                    db.execute("DELETE FROM lease WHERE k=?", (key,))
                # A pre-denial response must be invalidated atomically,
                # including when the original owner no longer holds the key.
                db.execute(
                    "INSERT OR REPLACE INTO cache(k,value,until,host) "
                    "VALUES(?,?,?,?)",
                    (key, json.dumps({"stale": True}), time.time() + 600, HOST),
                )
                db.commit()
            return Response()

        def close(self):
            pass

    transport._thread.session = Session()
    try:
        with pytest.raises(SourceAccessDenied) as exc:
            transport.get(URL, PARAMS)
        assert (exc.value.host, exc.value.http_status) == (HOST, status)
        assert old_owner and old_owner[0] != "new-worker"
        assert observer.denied_status(HOST) == status
        with pytest.raises(ProviderDenied):
            observer.read_fresh(URL, PARAMS, HOST)
        with observer.db() as db:
            assert db.execute(
                "SELECT COUNT(*) FROM cache WHERE host=?", (HOST,)
            ).fetchone()[0] == 0
            lease = db.execute("SELECT owner FROM lease LIMIT 1").fetchone()
        assert lease == (("new-worker",) if successor else None)
    finally:
        transport.close()
        observer.close()


def test_denial_evidence_preserves_451_and_other_hosts(tmp_path):
    cache = PublicCache(tmp_path / "public.sqlite")
    try:
        other_key, other_owner, _ = cache.begin(OTHER_URL, {}, OTHER_HOST)
        cache.finish(other_key, other_owner, OTHER_HOST, {"ok": True}, ttl=600)
        first, first_owner, _ = cache.begin(URL, PARAMS, HOST)
        second_url = "https://api.bybit.com/v5/market/instruments-info"
        second, second_owner, _ = cache.begin(second_url, {}, HOST)
        cache.record_http_denial(
            first, first_owner, HOST, cooldown=3600, denial_status=451
        )
        cache.record_http_denial(
            second, second_owner, HOST, cooldown=120, denial_status=403
        )
        assert cache.denied_status(HOST) == 451
        assert cache.read_fresh(OTHER_URL, {}, OTHER_HOST) == {"ok": True}
        with cache.db() as db:
            assert db.execute(
                "SELECT COUNT(*) FROM cache WHERE host=?", (HOST,)
            ).fetchone()[0] == 0
    finally:
        cache.close()


def test_unobserved_wrong_owner_finish_still_cannot_poison_fleet(tmp_path):
    cache = PublicCache(tmp_path / "public.sqlite")
    try:
        key, owner, _ = cache.begin(URL, PARAMS, HOST)
        cache.finish(key, "not-the-owner", HOST, cooldown=3600, denial_status=451)
        assert cache.denied_status(HOST) is None
        with cache.db() as db:
            assert db.execute("SELECT owner FROM lease WHERE k=?", (key,)).fetchone() == (owner,)
        with pytest.raises(ValueError):
            cache.record_http_denial(
                key, owner, HOST, cooldown=3600, denial_status=200
            )
        assert cache.denied_status(HOST) is None
    finally:
        cache.close()
