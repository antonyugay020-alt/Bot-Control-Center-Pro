"""Generation-fenced Hub recovery across Windows PID reuse and stale metadata.

Offline PAPER-only: no network, credentials, account mutations or orders.
A stale PID-only record cannot authorize deleting a live successor's lease.
"""
import os
import time

from bcc.hub_supervisor import _revoke_dead_owner
from bcc.market_hub import HubRegistry


def _current_owner(registry):
    with registry._connect() as db:
        return db.execute(
            "SELECT owner, lease_token FROM hub_leader WHERE singleton=1"
        ).fetchone()


def test_dead_record_reclaims_only_exact_owner_generation(tmp_path):
    registry = HubRegistry(tmp_path / "market.sqlite")
    pid = os.getpid()
    live_owner = f"{pid}:{'b' * 32}"
    stale_record = {"pid": pid, "leader_owner": f"{pid}:{'a' * 32}"}
    assert registry.claim_leader(live_owner, lifetime=120)
    before = _current_owner(registry)

    # Same numeric PID, different producer generation. A LIKE pid:% DELETE
    # would incorrectly kill this live lease after PID reuse on Windows.
    assert _revoke_dead_owner(registry, stale_record) is False
    assert _current_owner(registry) == before

    # The old PID-only format has no proof of lease ownership and must wait
    # for its TTL instead of revoking a possibly unrelated successor.
    assert _revoke_dead_owner(registry, {"pid": pid}) is False
    assert _revoke_dead_owner(registry, {
        "pid": pid, "leader_owner": f"{pid}:not-a-generation"
    }) is False
    assert _current_owner(registry) == before

    assert _revoke_dead_owner(registry, {
        "pid": pid, "leader_owner": live_owner
    }) is True
    assert _current_owner(registry) is None


def test_old_record_cannot_revoke_reclaimed_same_pid_lease(tmp_path):
    registry = HubRegistry(tmp_path / "market.sqlite")
    pid = os.getpid()
    old_owner = f"{pid}:{'c' * 32}"
    successor = f"{pid}:{'d' * 32}"
    assert registry.claim_leader(old_owner, lifetime=120)
    with registry._connect() as db:
        db.execute(
            "UPDATE hub_leader SET expires=? WHERE singleton=1",
            (time.time() - 5,),
        )
    assert registry.claim_leader(successor, lifetime=120)
    token = registry.leader_token(successor)
    assert token
    assert not _revoke_dead_owner(
        registry, {"pid": pid, "leader_owner": old_owner}
    )
    assert registry.leader_token(successor) == token
    assert _current_owner(registry)[0] == successor
