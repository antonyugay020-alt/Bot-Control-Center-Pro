from bcc.market_hub import HubRegistry

URL = "https://api.bybit.com/v5/market/orderbook"


def test_stale_owner_cannot_acknowledge_after_handover(tmp_path):
    registry = HubRegistry(tmp_path / "hub.sqlite")
    key = registry.subscribe(URL, {"symbol": "BTCUSDT"})
    assert registry.claim_leader("old")
    assert registry.finish_owned(key, 1, "old")
    registry.release_leader("old")
    assert registry.claim_leader("new")
    assert not registry.finish_owned(key, 500, "old", "stale")
    with registry._connect() as db:
        row = db.execute("SELECT last_error FROM hub_subscriptions WHERE k=?", (key,)).fetchone()
    assert row[0] == ""
    assert registry.finish_owned(key, 3, "new", "new-owner")
    with registry._connect() as db:
        row = db.execute("SELECT last_error FROM hub_subscriptions WHERE k=?", (key,)).fetchone()
    assert row[0] == "new-owner"
