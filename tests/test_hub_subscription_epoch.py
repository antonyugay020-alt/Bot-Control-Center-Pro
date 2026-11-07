"""Subscription incarnation fencing across Hub restarts and old HTTP futures.

Offline PAPER-only SQLite tests. No public HTTP, credentials or orders.
"""
import sqlite3
import threading
import time

from bcc.market_hub import HubRegistry, MarketDataHub


URL = "https://api.bybit.com/v5/market/orderbook"
PARAMS = {"category": "linear", "symbol": "BTCUSDT"}


def generation(registry, key):
    with registry._connect() as db:
        return db.execute(
            "SELECT subscription_token FROM hub_subscriptions WHERE k=?", (key,)
        ).fetchone()[0]


def test_active_renewal_preserves_incarnation_but_revival_fences_old_ack(tmp_path):
    registry = HubRegistry(tmp_path / "hub.sqlite")
    key = registry.subscribe(URL, PARAMS)
    old = generation(registry, key)
    registry.subscribe(URL, PARAMS, lifetime=300)
    assert generation(registry, key) == old
    assert registry.claim_leader("producer")
    leader = registry.leader_token("producer")

    with registry._connect() as db:
        db.execute(
            "UPDATE hub_subscriptions SET active_until=?,next_due=0 WHERE k=?",
            (time.time() - 1, key),
        )
    # A fetch started before expiry must not acknowledge the next incarnation.
    registry.subscribe(URL, PARAMS)
    current = generation(registry, key)
    assert current and current != old
    assert not registry.finish_owned(
        key, 500, "producer", "stale HTTP", token=leader,
        subscription_token=old,
    )
    assert registry.due(include_generation=True) == [
        (key, URL, PARAMS, current)
    ]
    assert registry.finish_owned(
        key, 2, "producer", token=leader, subscription_token=current,
    )
    assert registry.due(include_generation=True) == []


def test_pruned_and_recreated_subscription_rejects_old_ack(tmp_path):
    registry = HubRegistry(tmp_path / "hub.sqlite")
    key = registry.subscribe(URL, PARAMS)
    old = generation(registry, key)
    assert registry.claim_leader("producer")
    leader = registry.leader_token("producer")
    with registry._connect() as db:
        db.execute(
            "UPDATE hub_subscriptions SET active_until=? WHERE k=?",
            (time.time() - 7200, key),
        )
    assert registry.prune_expired() == 1
    assert registry.subscribe(URL, PARAMS) == key
    assert generation(registry, key) != old
    assert not registry.finish_owned(
        key, 500, "producer", "old key", token=leader,
        subscription_token=old,
    )
    assert registry.due(include_generation=True)[0][0] == key


def test_migrated_legacy_subscription_gets_persistent_incarnation(tmp_path):
    path = tmp_path / "hub.sqlite"
    key, encoded = HubRegistry.identity(URL, PARAMS)
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE hub_subscriptions("
            "k TEXT PRIMARY KEY,url TEXT NOT NULL,params TEXT NOT NULL,"
            "active_until REAL NOT NULL,next_due REAL NOT NULL DEFAULT 0,"
            "last_error TEXT NOT NULL DEFAULT '',"
            "priority_until REAL NOT NULL DEFAULT 0)"
        )
        db.execute(
            "INSERT INTO hub_subscriptions(k,url,params,active_until,next_due)"
            " VALUES(?,?,?,?,0)", (key, URL, encoded, time.time() + 60),
        )
    registry = HubRegistry(path)
    old = generation(registry, key)
    assert len(old) == 32
    assert registry.due(include_generation=True)[0][-1] == old
    registry = HubRegistry(path)
    assert generation(registry, key) == old


def test_nonblocking_producer_does_not_ack_revived_subscriber(tmp_path):
    path = tmp_path / "hub.sqlite"
    registry = HubRegistry(path)
    key = registry.subscribe(URL, PARAMS)
    first_generation = generation(registry, key)
    started = threading.Event()
    release = threading.Event()
    calls = []

    class Source:
        shared = object()

        def get(self, url, params=None):
            calls.append((url, params))
            if len(calls) == 1:
                started.set()
                assert release.wait(5), "synthetic HTTP blocked too long"

        def close(self):
            pass

    hub = MarketDataHub(path, Source(), max_workers=2)
    try:
        hub.poll_nonblocking()
        assert started.wait(3)
        with registry._connect() as db:
            db.execute(
                "UPDATE hub_subscriptions SET active_until=? WHERE k=?",
                (time.time() - 1, key),
            )
        registry.subscribe(URL, PARAMS)
        current = generation(registry, key)
        assert current != first_generation
        release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            hub.poll_nonblocking()
            with registry._connect() as db:
                due = db.execute(
                    "SELECT next_due FROM hub_subscriptions WHERE k=?", (key,)
                ).fetchone()[0]
            if due > time.time():
                break
            time.sleep(.02)
        assert due > time.time(), "new incarnation was not independently fetched"
        assert len(calls) >= 2
    finally:
        release.set()
        hub.close()

def test_every_hub_completion_callsite_carries_both_fencing_tokens():
    """A future producer path must not silently omit subscriber incarnation."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for directory in ("bcc", "tools", "tests"):
        for source in (root / directory).glob("*.py"):
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "finish_owned"):
                    keywords = {item.arg for item in node.keywords}
                    assert {"token", "subscription_token"} <= keywords, (
                        f"Unfenced Hub completion: {source}:{node.lineno}"
                    )
