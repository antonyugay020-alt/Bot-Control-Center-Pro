"""Hub leases fence producer generations and independent PAPER subscribers."""

import sqlite3
import threading
import time

from bcc.market_hub import HubRegistry, MarketDataHub

URL = "https://api.bybit.com/v5/market/orderbook"


def _subscription(registry, key):
    with registry._connect() as db:
        return db.execute(
            "SELECT active_until,next_due,last_error,priority_until "
            "FROM hub_subscriptions WHERE k=?", (key,)
        ).fetchone()


def test_released_and_reclaimed_same_owner_cannot_acknowledge_old_work(tmp_path):
    registry = HubRegistry(tmp_path / "hub.sqlite")
    key = registry.subscribe(URL, {"symbol": "BTCUSDT"})
    assert registry.claim_leader("producer")
    first = registry.leader_token("producer")
    assert first
    assert registry.claim_leader("producer")  # ordinary renewal is same epoch
    assert registry.leader_token("producer") == first
    registry.release_leader("producer", token=first)
    assert registry.claim_leader("producer")
    second = registry.leader_token("producer")
    assert second and second != first
    assert not registry.finish_owned(key, 500, "producer", "stale", token=first)
    assert _subscription(registry, key)[1:3] == (0, "")
    assert registry.finish_owned(key, 2, "producer", "new", token=second)
    assert _subscription(registry, key)[2] == "new"


def test_legacy_unowned_completion_cannot_bypass_lease_fence(tmp_path):
    registry = HubRegistry(tmp_path / "hub.sqlite")
    key = registry.subscribe(URL, {"symbol": "BTCUSDT"})
    try:
        registry.finish(key, 600, "unowned")
    except RuntimeError as exc:
        assert "Unfenced" in str(exc)
    else:
        raise AssertionError("unowned hub completion was accepted")
    assert _subscription(registry, key)[1:3] == (0, "")


def test_expired_same_owner_mints_new_fencing_generation(tmp_path):
    registry = HubRegistry(tmp_path / "hub.sqlite")
    key = registry.subscribe(URL, {"symbol": "ETHUSDT"})
    assert registry.claim_leader("producer")
    first = registry.leader_token("producer")
    with registry._connect() as db:
        db.execute("UPDATE hub_leader SET expires=0 WHERE singleton=1")
    assert registry.claim_leader("producer")
    second = registry.leader_token("producer")
    assert second and second != first
    assert not registry.finish_owned(key, 120, "producer", token=first)
    assert registry.finish_owned(key, 1, "producer", token=second)


def test_legacy_leader_table_is_migrated_without_reusing_empty_token(tmp_path):
    path = tmp_path / "hub.sqlite"
    db = sqlite3.connect(path)
    try:
        db.execute(
            "CREATE TABLE hub_leader(singleton INTEGER PRIMARY KEY, "
            "owner TEXT NOT NULL, expires REAL NOT NULL)"
        )
        db.execute("INSERT INTO hub_leader VALUES(1,?,?)",
                   ("legacy", time.time() + 60))
        db.commit()
    finally:
        db.close()
    registry = HubRegistry(path)
    assert registry.claim_leader("legacy")
    token = registry.leader_token("legacy")
    assert isinstance(token, str) and len(token) == 32
    key = registry.subscribe(URL, {"symbol": "SOLUSDT"})
    assert registry.finish_owned(key, 1, "legacy", token=token)


def test_shared_subscribers_cannot_shorten_each_others_lifetime(tmp_path):
    registry = HubRegistry(tmp_path / "hub.sqlite")
    key = registry.subscribe(URL, {"symbol": "BTCUSDT"},
                             lifetime=300, priority=True)
    original = _subscription(registry, key)
    # Another bot with a short request lease must not retire the shared
    # subscription or downgrade urgency while the first bot still needs it.
    assert registry.subscribe(URL, {"symbol": "BTCUSDT"},
                              lifetime=2, priority=False) == key
    updated = _subscription(registry, key)
    assert updated[0] >= original[0]
    assert updated[3] >= original[3]


def test_nonblocking_hub_discards_old_epoch_future_before_ack(tmp_path):
    path = tmp_path / "hub.sqlite"
    registry = HubRegistry(path)
    key = registry.subscribe(URL, {"symbol": "BTCUSDT"})
    entered = threading.Event()
    release = threading.Event()
    calls = []
    class Source:
        shared = object()
        def get(self, url, params=None):
            calls.append(params["symbol"])
            if len(calls) == 1:
                entered.set()
                assert release.wait(10), "timed out waiting to finish old request"
        def close(self):
            pass

    hub = MarketDataHub(path, Source(), max_workers=2)
    try:
        hub.poll_nonblocking()
        assert entered.wait(5), "first HTTP task was not scheduled"
        first = registry.leader_token(hub.owner)
        assert first
        registry.release_leader(hub.owner, token=first)
        assert registry.claim_leader(hub.owner)
        assert registry.leader_token(hub.owner) != first
        release.set()
        deadline = time.monotonic() + 5
        while not hub._inflight[key][0].done() and time.monotonic() < deadline:
            time.sleep(.01)
        assert hub._inflight[key][0].done()
        hub._leader_check_due = 0
        hub.poll_nonblocking()  # stale completion is discarded; fresh fetch queued
        assert _subscription(registry, key)[1:3] == (0, "")
        assert len(calls) <= 2
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            hub.poll_nonblocking()
            if _subscription(registry, key)[1] > 0:
                break
            time.sleep(.01)
        assert _subscription(registry, key)[1] > 0
        assert len(calls) >= 2
    finally:
        release.set()
        hub.close()


def test_old_generation_cannot_renew_or_revoke_reclaimed_same_owner(tmp_path):
    """Fencing applies to heartbeats and shutdown, not only to HTTP acks."""
    registry = HubRegistry(tmp_path / "hub.sqlite")
    assert registry.claim_leader("producer")
    first = registry.leader_token("producer")
    assert registry.release_leader("producer", token=first)
    assert registry.claim_leader("producer")
    second = registry.leader_token("producer")
    assert second != first
    with registry._connect() as db:
        expires_before = db.execute(
            "SELECT expires FROM hub_leader WHERE singleton=1"
        ).fetchone()[0]
    assert not registry.renew_leader("producer", token=first)
    assert not registry.release_leader("producer", token=first)
    with registry._connect() as db:
        assert db.execute(
            "SELECT expires,lease_token FROM hub_leader WHERE singleton=1"
        ).fetchone() == (expires_before, second)
    assert registry.renew_leader("producer", token=second)
    assert registry.release_leader("producer", token=second)


def test_hub_close_releases_only_the_acquired_generation(tmp_path):
    path = tmp_path / "hub.sqlite"
    registry = HubRegistry(path)
    registry.subscribe(URL, {"symbol": "BTCUSDT"})

    class Source:
        shared = object()
        def get(self, url, params=None):
            return {"ok": True}
        def close(self):
            pass

    hub = MarketDataHub(path, Source())
    assert hub.run_once()
    old = registry.leader_token(hub.owner)
    assert old
    assert registry.release_leader(hub.owner, token=old)
    assert registry.claim_leader(hub.owner)
    new = registry.leader_token(hub.owner)
    assert new != old
    hub.close()
    assert registry.leader_token(hub.owner) == new
    assert registry.release_leader(hub.owner, token=new)


def test_all_hub_lease_mutations_in_source_are_generation_scoped():
    """Reject unscoped callsites throughout the integrated Python tree."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for directory in ("bcc", "tools", "tests"):
        for source in (root / directory).glob("*.py"):
            module = ast.parse(source.read_text(encoding="utf-8"),
                               filename=str(source))
            for node in ast.walk(module):
                if not isinstance(node, ast.Call):
                    continue
                if not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr not in ("renew_leader", "release_leader"):
                    continue
                assert any(kw.arg == "token" for kw in node.keywords), (
                    f"Unfenced Hub lease mutation: {source}:{node.lineno}"
                )
