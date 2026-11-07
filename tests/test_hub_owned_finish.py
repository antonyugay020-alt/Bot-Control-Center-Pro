from bcc.market_hub import HubRegistry

URL = "https://api.bybit.com/v5/market/orderbook"


def test_stale_owner_cannot_acknowledge_after_handover(tmp_path):
    registry = HubRegistry(tmp_path / "hub.sqlite")
    key = registry.subscribe(URL, {"symbol": "BTCUSDT"})
    generation = registry.due(include_generation=True)[0][-1]
    assert registry.claim_leader("old")
    old_token = registry.leader_token("old")
    assert registry.finish_owned(key, 1, "old", token=old_token, subscription_token=generation)
    registry.release_leader("old", token=old_token)
    assert registry.claim_leader("new")
    new_token = registry.leader_token("new")
    assert not registry.finish_owned(key, 500, "old", "stale", token=old_token, subscription_token=generation)
    with registry._connect() as db:
        row = db.execute("SELECT last_error FROM hub_subscriptions WHERE k=?", (key,)).fetchone()
    assert row[0] == ""
    assert registry.finish_owned(key, 3, "new", "new-owner", token=new_token, subscription_token=generation)
    with registry._connect() as db:
        row = db.execute("SELECT last_error FROM hub_subscriptions WHERE k=?", (key,)).fetchone()
    assert row[0] == "new-owner"
