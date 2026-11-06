"""Cross-process policy fences must survive SQLite migrations and propagate
typed denials through every worker path without treating them as hub lag.
These tests never contact an exchange or use trading credentials.
"""

import sqlite3
import time

import pytest

import bcc.data_cache as cache_module
from bcc.data_cache import ProviderDenied, PublicCache
from bcc.market import PublicData, SourceAccessDenied
from bcc.market_hub import HubRegistry


HOST = "api.bybit.com"
URL = "https://api.bybit.com/v5/market/tickers"
PARAMS = {"category": "linear", "symbol": "BTCUSDT"}


def _fence(cache, status=451):
    key, owner, result = cache.begin(URL, PARAMS, HOST)
    assert owner and result is None
    cache.finish(key, owner, HOST, cooldown=3600, denial_status=status)
    return key


@pytest.mark.parametrize("status", [403, 451])
def test_shared_policy_refusal_preserves_status_for_reader(tmp_path, status):
    path = tmp_path / "fleet.sqlite"
    writer, reader = PublicCache(path), PublicCache(path)
    try:
        _fence(writer, status)
        for action in (
            lambda: reader.begin(URL, PARAMS, HOST),
            lambda: reader.read_fresh(URL, PARAMS, HOST),
        ):
            with pytest.raises(ProviderDenied) as exc:
                action()
            assert (exc.value.host, exc.value.http_status) == (HOST, status)
    finally:
        writer.close()
        reader.close()


def test_existing_three_column_policy_table_migrates_without_losing_denial(tmp_path):
    path = tmp_path / "fleet.sqlite"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE provider_denial("
            "host TEXT PRIMARY KEY,status INTEGER NOT NULL,until_mono REAL NOT NULL)"
        )
        db.execute(
            "INSERT INTO provider_denial VALUES(?,?,?)",
            (HOST, 451, time.monotonic() + 1000),
        )
    cache = PublicCache(path)
    try:
        assert cache.denied_status(HOST) == 451
        with cache.db() as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(provider_denial)")}
            recorded_mono, until_utc = db.execute(
                "SELECT recorded_mono,until_utc FROM provider_denial WHERE host=?",
                (HOST,),
            ).fetchone()
        assert {"recorded_mono", "until_utc"} <= columns
        assert recorded_mono > 0 and until_utc > time.time()
        with pytest.raises(ProviderDenied) as exc:
            cache.begin(URL, PARAMS, HOST)
        assert exc.value.http_status == 451
    finally:
        cache.close()


def test_denial_clock_jump_and_reboot_expiry_use_correct_clock(tmp_path, monkeypatch):
    cache = PublicCache(tmp_path / "fleet.sqlite")
    try:
        _fence(cache)
        with cache.db() as db:
            status, expiry_mono, recorded_mono, expiry_utc = db.execute(
                "SELECT status,until_mono,recorded_mono,until_utc "
                "FROM provider_denial WHERE host=?", (HOST,)
            ).fetchone()
        assert status == 451 and expiry_mono > recorded_mono
        with monkeypatch.context() as patch:
            # Same boot: wall-clock leap forward must not unlock the venue.
            patch.setattr(cache_module.time, "monotonic", lambda: recorded_mono + 1)
            patch.setattr(cache_module.time, "time", lambda: expiry_utc + 100000)
            assert cache.denied_status(HOST) == 451
            # A new boot resets monotonic uptime. Use persisted UTC deadline
            # to avoid applying the previous boot's uptime to the new boot.
            patch.setattr(cache_module.time, "monotonic", lambda: 0.0)
            patch.setattr(cache_module.time, "time", lambda: expiry_utc - 100)
            assert cache.denied_status(HOST) == 451
            patch.setattr(cache_module.time, "time", lambda: expiry_utc + 1)
            assert cache.denied_status(HOST) is None
    finally:
        cache.close()


def test_new_refusal_replaces_expired_previous_boot_deadline(tmp_path, monkeypatch):
    cache = PublicCache(tmp_path / "fleet.sqlite")
    try:
        _fence(cache)
        with cache.db() as db:
            expiry_utc = db.execute(
                "SELECT until_utc FROM provider_denial WHERE host=?", (HOST,)
            ).fetchone()[0]
        with monkeypatch.context() as patch:
            patch.setattr(cache_module.time, "monotonic", lambda: 10.0)
            patch.setattr(cache_module.time, "time", lambda: expiry_utc + 100)
            assert cache.denied_status(HOST) is None
            _fence(cache, status=403)
            with cache.db() as db:
                new_deadline = db.execute(
                    "SELECT until_mono FROM provider_denial WHERE host=?", (HOST,)
                ).fetchone()[0]
            assert new_deadline == pytest.approx(3610.0)
            assert cache.denied_status(HOST) == 403
    finally:
        cache.close()


def test_hub_wait_propagates_policy_refusal_arriving_after_subscription(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BCC_MARKET_HUB_MODE", "client")
    path = tmp_path / "fleet.sqlite"
    producer = PublicCache(path)
    key, owner, _ = producer.begin(URL, PARAMS, HOST)
    consumer = PublicData()
    consumer.shared = PublicCache(path)
    subscriptions = []

    def refuse_on_subscribe(self, url, params, *, lifetime, priority):
        subscriptions.append(url)
        producer.finish(key, owner, HOST, cooldown=3600, denial_status=451)

    monkeypatch.setattr(HubRegistry, "subscribe", refuse_on_subscribe)
    try:
        with pytest.raises(SourceAccessDenied) as exc:
            consumer.get(URL, PARAMS)
        assert subscriptions == [URL]
        assert (exc.value.host, exc.value.http_status) == (HOST, 451)
    finally:
        consumer.close()
        producer.close()


def test_direct_lease_admission_propagates_racing_denial(tmp_path, monkeypatch):
    monkeypatch.delenv("BCC_MARKET_HUB_MODE", raising=False)
    path = tmp_path / "fleet.sqlite"
    producer = PublicCache(path)
    key, owner, _ = producer.begin(URL, PARAMS, HOST)
    consumer = PublicData()
    consumer.shared = PublicCache(path)
    real_begin = consumer.shared.begin

    def refuse_before_begin(*args, **kwargs):
        producer.finish(key, owner, HOST, cooldown=3600, denial_status=403)
        return real_begin(*args, **kwargs)

    monkeypatch.setattr(consumer.shared, "begin", refuse_before_begin)
    try:
        with pytest.raises(SourceAccessDenied) as exc:
            consumer.get(URL, PARAMS)
        assert (exc.value.host, exc.value.http_status) == (HOST, 403)
    finally:
        consumer.close()
        producer.close()


def test_local_cooldown_read_preserves_racing_policy_denial(tmp_path, monkeypatch):
    monkeypatch.delenv("BCC_MARKET_HUB_MODE", raising=False)
    path = tmp_path / "fleet.sqlite"
    producer = PublicCache(path)
    key, owner, _ = producer.begin(URL, PARAMS, HOST)
    consumer = PublicData()
    consumer.shared = PublicCache(path)
    consumer.cooldowns[HOST] = time.time() + 100
    real_read = consumer.shared.read_fresh

    def refuse_before_read(*args, **kwargs):
        producer.finish(key, owner, HOST, cooldown=3600, denial_status=451)
        return real_read(*args, **kwargs)

    monkeypatch.setattr(consumer.shared, "read_fresh", refuse_before_read)
    try:
        with pytest.raises(SourceAccessDenied) as exc:
            consumer.get(URL, PARAMS)
        assert (exc.value.host, exc.value.http_status) == (HOST, 451)
    finally:
        consumer.close()
        producer.close()


def test_policy_refusal_invalidates_only_rejected_host_cache(tmp_path, monkeypatch):
    path = tmp_path / "fleet.sqlite"
    cache = PublicCache(path)
    other_host = "gamma-api.polymarket.com"
    other_url = "https://gamma-api.polymarket.com/markets"
    denial_url = "https://api.bybit.com/v5/market/instruments-info"
    try:
        key, owner, _ = cache.begin(URL, PARAMS, HOST)
        cache.finish(key, owner, HOST, {"old": True}, ttl=7200)
        key, owner, _ = cache.begin(other_url, {}, other_host)
        cache.finish(key, owner, other_host, {"independent": True}, ttl=7200)
        key, owner, _ = cache.begin(denial_url, {}, HOST)
        cache.finish(key, owner, HOST, cooldown=3600, denial_status=451)
        with pytest.raises(ProviderDenied):
            cache.read_fresh(URL, PARAMS, HOST)
        assert cache.read_fresh(other_url, {}, other_host) == {"independent": True}
        with cache.db() as db:
            rows = db.execute("SELECT host FROM cache").fetchall()
            _, expiry_mono, recorded_mono, expiry_utc = db.execute(
                "SELECT status,until_mono,recorded_mono,until_utc "
                "FROM provider_denial WHERE host=?", (HOST,)
            ).fetchone()
        assert rows == [(other_host,)]
        with monkeypatch.context() as patch:
            patch.setattr(cache_module.time, "monotonic", lambda: expiry_mono + 1)
            patch.setattr(cache_module.time, "time", lambda: expiry_utc + 1)
            assert cache.denied_status(HOST) is None
            # An old quote must not reappear just because the fence expired.
            assert cache.read_fresh(URL, PARAMS, HOST) is None
            assert cache.begin(URL, PARAMS, HOST)[1] is not None
    finally:
        cache.close()


def test_inflight_success_cannot_repopulate_rejected_host_cache(tmp_path):
    cache = PublicCache(tmp_path / "fleet.sqlite")
    try:
        first_key, first_owner, _ = cache.begin(URL, PARAMS, HOST)
        other_url = "https://api.bybit.com/v5/market/instruments-info"
        second_key, second_owner, _ = cache.begin(other_url, {}, HOST)
        cache.finish(second_key, second_owner, HOST, cooldown=3600, denial_status=403)
        cache.finish(first_key, first_owner, HOST, {"stale": True}, ttl=7200)
        with cache.db() as db:
            assert db.execute(
                "SELECT count(*) FROM cache WHERE host=?", (HOST,)
            ).fetchone()[0] == 0
    finally:
        cache.close()


def test_legacy_cache_rows_without_host_are_never_reused(tmp_path):
    import hashlib
    import json

    path = tmp_path / "fleet.sqlite"
    key = hashlib.sha256(json.dumps([URL, PARAMS], sort_keys=True).encode()).hexdigest()
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE cache(k TEXT PRIMARY KEY,value TEXT NOT NULL,until REAL NOT NULL)"
        )
        db.execute(
            "INSERT INTO cache VALUES(?,?,?)",
            (key, json.dumps({"old": True}), time.time() + 7200),
        )
    cache = PublicCache(path)
    try:
        assert cache.read_fresh(URL, PARAMS, HOST) is None
        with cache.db() as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(cache)")}
        assert "host" in columns
    finally:
        cache.close()


def test_shared_worker_ignores_unowned_memory_cache(tmp_path, monkeypatch):
    import json

    monkeypatch.delenv("BCC_MARKET_HUB_MODE", raising=False)
    transport = PublicData()
    transport.shared = PublicCache(tmp_path / "fleet.sqlite")
    transport.cache[(URL, json.dumps(PARAMS, sort_keys=True))] = (
        time.time(), {"old": True}
    )

    class Response:
        status_code = 200
        headers = {}

        def raise_for_status(self):
            pass

        def json(self):
            return {"new": True}

    class Session:
        def get(self, *args, **kwargs):
            return Response()

        def close(self):
            pass

    transport._thread.session = Session()
    try:
        assert transport.get(URL, PARAMS, ttl=3600) == {"new": True}
        assert transport._thread.last_read["via"] == "http"
    finally:
        transport.close()


def test_successful_http_cannot_override_racing_policy_refusal(tmp_path, monkeypatch):
    monkeypatch.delenv("BCC_MARKET_HUB_MODE", raising=False)
    path = tmp_path / "fleet.sqlite"
    producer = PublicCache(path)
    consumer = PublicData()
    consumer.shared = PublicCache(path)

    class Response:
        status_code = 200
        headers = {}

        def raise_for_status(self):
            pass

        def json(self):
            return {"valid": True}

    class Session:
        def get(self, *args, **kwargs):
            url = "https://api.bybit.com/v5/market/instruments-info"
            key, owner, _ = producer.begin(url, {}, HOST)
            producer.finish(key, owner, HOST, cooldown=3600, denial_status=451)
            return Response()

        def close(self):
            pass

    consumer._thread.session = Session()
    try:
        with pytest.raises(SourceAccessDenied) as exc:
            consumer.get(URL, PARAMS, ttl=3600)
        assert (exc.value.host, exc.value.http_status) == (HOST, 451)
        with producer.db() as db:
            assert db.execute(
                "SELECT count(*) FROM cache WHERE host=?", (HOST,)
            ).fetchone()[0] == 0
    finally:
        consumer.close()
        producer.close()
