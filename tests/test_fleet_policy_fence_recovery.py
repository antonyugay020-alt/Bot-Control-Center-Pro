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
