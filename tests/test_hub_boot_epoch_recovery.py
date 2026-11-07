"""Boot-epoch-fenced Hub ownership and supervisor admission.

A persisted SQLite leader TTL is not evidence that its producer survived a
Windows reboot. All tests are offline PAPER-only: no HTTP, keys or orders.
"""
import sqlite3
import time

from bcc.market_hub import HubRegistry


URL = "https://api.bybit.com/v5/market/tickers"
PARAMS = {"category": "linear", "symbol": "BTCUSDT"}
OLD_BOOT = 1_700_000_000.0
NEW_BOOT = OLD_BOOT + 3600.0


def _set_boot(monkeypatch, boot):
    monkeypatch.setattr(HubRegistry, "_boot_epoch", staticmethod(lambda: boot))


def test_previous_boot_cannot_certify_or_renew_leader_and_is_reclaimed(tmp_path, monkeypatch):
    _set_boot(monkeypatch, OLD_BOOT)
    registry = HubRegistry(tmp_path / "market.sqlite")
    first = "123:" + "a" * 32
    second = "456:" + "b" * 32
    key = registry.subscribe(URL, PARAMS)
    generation = registry.due(include_generation=True)[0][-1]
    assert registry.claim_leader(first, lifetime=120)
    old_token = registry.leader_token(first)
    assert old_token and registry.live_leader()[0] == first

    # Simulate an OS reboot without waiting for the old wall-clock expiry.
    _set_boot(monkeypatch, NEW_BOOT)
    assert registry.live_leader() is None
    assert registry.leader_token(first) is None
    assert not registry.renew_leader(first, token=old_token)
    assert not registry.finish_owned(
        key, 20, first, token=old_token, subscription_token=generation
    )
    assert registry.claim_leader(second, lifetime=120)
    new_token = registry.leader_token(second)
    assert new_token and new_token != old_token
    assert registry.live_leader()[0] == second
    assert not registry.release_leader(first, token=old_token)
    assert registry.finish_owned(
        key, 20, second, token=new_token, subscription_token=generation
    )


def test_same_boot_cannot_steal_live_leader_and_preserves_token(tmp_path, monkeypatch):
    _set_boot(monkeypatch, OLD_BOOT)
    registry = HubRegistry(tmp_path / "market.sqlite")
    assert registry.claim_leader("first", lifetime=120)
    token = registry.leader_token("first")
    assert token
    assert not registry.claim_leader("second", lifetime=120)
    assert registry.claim_leader("first", lifetime=120)
    assert registry.leader_token("first") == token
    assert registry.renew_leader("first", token=token)
    assert registry.live_leader()[0] == "first"


def test_missing_boot_witness_is_conservative_until_ttl(tmp_path, monkeypatch):
    _set_boot(monkeypatch, 0.0)
    registry = HubRegistry(tmp_path / "market.sqlite")
    assert registry.claim_leader("legacy", lifetime=120)
    _set_boot(monkeypatch, NEW_BOOT)
    # A missing witness cannot prove a reboot; retain the bounded TTL.
    assert not registry.claim_leader("other", lifetime=120)
    with registry._connect() as db:
        db.execute("UPDATE hub_leader SET expires=? WHERE singleton=1",
                   (time.time() - 1,))
    assert registry.claim_leader("other", lifetime=120)
    assert registry.live_leader()[0] == "other"


def test_legacy_leader_schema_migrates_without_stealing_lease(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE hub_leader("
            "singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
            "owner TEXT NOT NULL,expires REAL NOT NULL)"
        )
        db.execute("INSERT INTO hub_leader VALUES(1,?,?)",
                   ("legacy", time.time() + 120))
    _set_boot(monkeypatch, OLD_BOOT)
    registry = HubRegistry(path)
    with registry._connect() as db:
        row = db.execute(
            "SELECT lease_token,boot_epoch FROM hub_leader WHERE singleton=1"
        ).fetchone()
    assert row == ("", 0.0)
    assert not registry.claim_leader("other", lifetime=120)
    # Same owner can adopt its legacy lease and mint a generation.
    assert registry.claim_leader("legacy", lifetime=120)
    assert registry.leader_token("legacy")
    assert registry.live_leader()[3] == OLD_BOOT
    _set_boot(monkeypatch, NEW_BOOT)
    assert registry.live_leader() is None
    assert registry.claim_leader("successor", lifetime=120)
    assert registry.live_leader()[0] == "successor"
