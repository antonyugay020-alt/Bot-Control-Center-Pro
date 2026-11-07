"""Transaction-time fencing of shared Market Data Hub leases and subscribers.

A Hub call may queue behind another SQLite writer. Reading wall time before
that queue is not a valid proof that an expiring lease/subscription still
exists when the SQL transaction finally executes. The controlled clock shift
below models that wait without sleeps, network access, keys, or orders.
"""

from contextlib import contextmanager

import pytest

import bcc.market_hub as hub_module
from bcc.market_hub import HubRegistry


URL = "https://api.bybit.com/v5/market/orderbook"
PARAMS = {"category": "linear", "symbol": "BTCUSDT"}


@pytest.fixture
def queued_registry(tmp_path, monkeypatch):
    registry = HubRegistry(tmp_path / "market.sqlite")
    clock = [1000.0]
    monkeypatch.setattr(hub_module.time, "time", lambda: clock[0])
    original_connect = registry._connect

    def queue_one_writer():
        @contextmanager
        def connect_after_wait():
            with original_connect() as db:
                # Represents five seconds queued for a database connection /
                # writer. Old code sampled time before _connect and therefore
                # incorrectly used 1000 instead of transaction time 1005.
                clock[0] += 5
                yield db

        monkeypatch.setattr(registry, "_connect", connect_after_wait)

    return registry, clock, original_connect, queue_one_writer


def test_contender_can_claim_lease_that_expired_while_queued(queued_registry):
    registry, clock, raw, queue = queued_registry
    with raw() as db:
        db.execute(
            "INSERT INTO hub_leader(singleton,owner,expires,lease_token) "
            "VALUES(1,'old',1002,'old-generation')"
        )
    queue()
    assert registry.claim_leader("successor", lifetime=15)
    with raw() as db:
        owner, expiry = db.execute(
            "SELECT owner,expires FROM hub_leader WHERE singleton=1"
        ).fetchone()
    assert owner == "successor"
    assert expiry == pytest.approx(1020)
    assert clock[0] == 1005


def test_expired_heartbeat_cannot_resurrect_lease_after_wait(queued_registry):
    registry, _, raw, queue = queued_registry
    with raw() as db:
        db.execute(
            "INSERT INTO hub_leader(singleton,owner,expires,lease_token) "
            "VALUES(1,'producer',1002,'old-generation')"
        )
    queue()
    assert not registry.renew_leader(
        "producer", token="old-generation", lifetime=15
    )
    with raw() as db:
        assert db.execute(
            "SELECT expires FROM hub_leader WHERE singleton=1"
        ).fetchone()[0] == 1002


def test_old_completion_cannot_ack_expired_subscription_after_wait(queued_registry):
    registry, _, raw, queue = queued_registry
    key = registry.subscribe(URL, PARAMS)
    with raw() as db:
        generation = db.execute(
            "SELECT subscription_token FROM hub_subscriptions WHERE k=?",
            (key,),
        ).fetchone()[0]
        db.execute(
            "UPDATE hub_subscriptions SET active_until=1002,next_due=0 "
            "WHERE k=?", (key,)
        )
        db.execute(
            "INSERT INTO hub_leader(singleton,owner,expires,lease_token) "
            "VALUES(1,'producer',2000,'leader-generation')"
        )
    queue()
    assert not registry.finish_owned(
        key, 500, "producer", token="leader-generation",
        subscription_token=generation,
    )
    with raw() as db:
        assert db.execute(
            "SELECT next_due FROM hub_subscriptions WHERE k=?", (key,)
        ).fetchone()[0] == 0


def test_renewal_rotates_expired_subscription_generation_at_commit(queued_registry):
    registry, _, raw, queue = queued_registry
    key = registry.subscribe(URL, PARAMS)
    with raw() as db:
        previous = db.execute(
            "SELECT subscription_token FROM hub_subscriptions WHERE k=?",
            (key,),
        ).fetchone()[0]
        db.execute(
            "UPDATE hub_subscriptions SET active_until=1002,next_due=1500 "
            "WHERE k=?", (key,)
        )
    queue()
    assert registry.subscribe(URL, PARAMS, lifetime=10, priority=True) == key
    with raw() as db:
        generation, active_until, next_due, priority_until = db.execute(
            "SELECT subscription_token,active_until,next_due,priority_until "
            "FROM hub_subscriptions WHERE k=?", (key,)
        ).fetchone()
    assert generation != previous
    assert active_until == pytest.approx(1015)
    assert next_due == 0
    assert priority_until == pytest.approx(1012.5)


def test_dequeue_rejects_subscriber_expired_during_connection_wait(queued_registry):
    registry, _, raw, queue = queued_registry
    key = registry.subscribe(URL, PARAMS)
    with raw() as db:
        db.execute(
            "UPDATE hub_subscriptions SET active_until=1002,next_due=0 "
            "WHERE k=?", (key,)
        )
    queue()
    assert registry.due(include_generation=True) == []


def test_expiry_cleanup_uses_transaction_clock_not_stale_queue_time(queued_registry):
    registry, _, raw, queue = queued_registry
    key = registry.subscribe(URL, PARAMS)
    with raw() as db:
        db.execute(
            "UPDATE hub_subscriptions SET active_until=942 WHERE k=?", (key,)
        )
    queue()
    assert registry.prune_expired(retention=60) == 1
    with raw() as db:
        assert db.execute(
            "SELECT 1 FROM hub_subscriptions WHERE k=?", (key,)
        ).fetchone() is None
