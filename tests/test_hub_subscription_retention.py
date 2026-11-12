"""Durable Hub subscriptions: renewal, revival, reclamation, and leader isolation.

All tests use local SQLite and PAPER-only synthetic sources. No HTTP or orders.
"""
import time

import pytest

from bcc.data_cache import PublicCache
from bcc.market_hub import HubRegistry, MarketDataHub


URL = "https://api.bybit.com/v5/market/orderbook"


def params(symbol):
    return {"category": "linear", "symbol": symbol}


def state(registry, key):
    with registry._connect() as db:
        return db.execute(
            "SELECT active_until,next_due,last_error,priority_until "
            "FROM hub_subscriptions WHERE k=?", (key,)
        ).fetchone()


def test_active_renewal_keeps_scheduler_and_priority(tmp_path):
    registry = HubRegistry(tmp_path / "market.sqlite")
    key = registry.subscribe(URL, params("BTCUSDT"), lifetime=60, priority=True)
    now = time.time()
    with registry._connect() as db:
        db.execute(
            "UPDATE hub_subscriptions SET next_due=?,last_error=? WHERE k=?",
            (now + 120, "upstream temporarily unavailable", key),
        )
    registry.subscribe(URL, params("BTCUSDT"), lifetime=60, priority=False)
    active_until, next_due, last_error, priority_until = state(registry, key)
    assert active_until >= now
    assert next_due == pytest.approx(now + 120)
    assert last_error == "upstream temporarily unavailable"
    assert priority_until > now


def test_expired_subscription_revives_immediately_without_old_priority(tmp_path):
    registry = HubRegistry(tmp_path / "market.sqlite")
    key = registry.subscribe(URL, params("BTCUSDT"), lifetime=60, priority=True)
    now = time.time()
    with registry._connect() as db:
        db.execute(
            "UPDATE hub_subscriptions SET active_until=?,next_due=?,"
            "last_error=?,priority_until=? WHERE k=?",
            (now - 2, now + 120, "old producer failure", now + 90, key),
        )
    registry.subscribe(URL, params("BTCUSDT"), lifetime=60, priority=False)
    active_until, next_due, last_error, priority_until = state(registry, key)
    assert active_until > now
    assert next_due == 0
    assert last_error == ""
    assert priority_until == 0
    assert (key, URL, params("BTCUSDT")) in registry.due(limit=8)


def test_bounded_reclamation_preserves_active_and_recent(tmp_path):
    registry = HubRegistry(tmp_path / "market.sqlite")
    symbols = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT")
    keys = [registry.subscribe(URL, params(symbol), lifetime=60) for symbol in symbols]
    now = time.time()
    with registry._connect() as db:
        db.executemany(
            "UPDATE hub_subscriptions SET active_until=? WHERE k=?",
            [(now - 7200, key) for key in keys[:3]] +
            [(now - 30, keys[3])],
        )
    assert registry.prune_expired(retention=3600, batch=2) == 2
    assert registry.prune_expired(retention=3600, batch=2) == 1
    assert registry.prune_expired(retention=3600, batch=2) == 0
    assert state(registry, keys[3]) is not None  # restart grace period
    assert state(registry, keys[4]) is not None  # active subscriber
    assert registry.has_active_subscriptions()
    with pytest.raises(ValueError):
        registry.prune_expired(retention=0)
    with pytest.raises(ValueError):
        registry.prune_expired(batch=0)


def test_only_admitted_hub_leader_reclaims_expired_rows(tmp_path, monkeypatch):
    path = tmp_path / "market.sqlite"
    registry = HubRegistry(path)
    key = registry.subscribe(URL, params("BTCUSDT"), lifetime=60)
    with registry._connect() as db:
        db.execute(
            "UPDATE hub_subscriptions SET active_until=? WHERE k=?",
            (time.time() - 7200, key),
        )

    class Source:
        def __init__(self):
            self.shared = PublicCache(path)

        def get(self, *args, **kwargs):
            raise AssertionError("Expired subscription must not trigger HTTP")

        def close(self):
            self.shared.close()

    leader = MarketDataHub(path, Source())
    follower = MarketDataHub(path, Source())
    try:
        assert leader.run_once() == []
        assert state(registry, key) is None

        def forbidden_cleanup(*args, **kwargs):
            raise AssertionError("Follower attempted Hub maintenance")

        monkeypatch.setattr(follower.registry, "prune_expired", forbidden_cleanup)
        assert follower.run_once() == []
    finally:
        follower.close()
        leader.close()
