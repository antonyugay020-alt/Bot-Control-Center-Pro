"""Integrated PAPER operations: fleet transfer, Hub lease, and provider denial.

These invariants must hold together in the *same* installed source tree. The
three feature branches are deliberately tested as an integrated architecture.
No market network calls or real orders are ever made.
"""

import zipfile

import pytest

from bcc.data_cache import PublicCache
from bcc.market import PublicData, SourceAccessDenied
from bcc.market_hub import HubRegistry
from bcc.storage import Repository, Store
from bcc.supervisor import Supervisor
from test_core import config


URL = "https://api.bybit.com/v5/market/tickers"
PARAMS = {"category": "linear", "symbol": "BTCUSDT"}
HOST = "api.bybit.com"


def test_independent_hub_fleet_transfer_and_access_denial(tmp_path, monkeypatch):
    repo = Repository(tmp_path / "app")
    accounts = [repo.create(config()), repo.create(config())]
    for account in accounts:
        Store(account, 500)
    supervisor = Supervisor(repo)

    shared_path = tmp_path / "public.sqlite"
    registry = HubRegistry(shared_path)
    subscription = registry.subscribe(URL, PARAMS, lifetime=300, priority=True)
    with registry._connect() as db:
        original_expiry = db.execute(
            "SELECT active_until FROM hub_subscriptions WHERE k=?",
            (subscription,),
        ).fetchone()[0]
    # A second bot's short-lived request must not retire the first bot.
    assert registry.subscribe(URL, PARAMS, lifetime=1) == subscription
    with registry._connect() as db:
        assert db.execute(
            "SELECT active_until FROM hub_subscriptions WHERE k=?",
            (subscription,),
        ).fetchone()[0] >= original_expiry

    assert registry.claim_leader("integration-producer")
    token = registry.leader_token("integration-producer")
    assert token

    producer = PublicCache(shared_path)
    client = PublicData()
    client.shared = PublicCache(shared_path)
    monkeypatch.setenv("BCC_MARKET_HUB_MODE", "client")
    try:
        cache_key, owner, cached = producer.begin(URL, PARAMS, HOST)
        assert owner and cached is None
        producer.finish(
            cache_key, owner, HOST, cooldown=3600, denial_status=451
        )
        with pytest.raises(SourceAccessDenied) as refusal:
            client.get(URL, PARAMS)
        assert (refusal.value.host, refusal.value.http_status) == (HOST, 451)

        # Public-feed denial must not damage independent PAPER state, nor
        # bypass the multi-owner snapshot barrier used by operator export.
        archive = tmp_path / "fleet.zip"
        supervisor.export_bots(accounts, archive, history=True)
        with zipfile.ZipFile(archive) as stream:
            paths = set(stream.namelist())
        assert "bot_0000/paper.sqlite" in paths
        assert "bot_0001/paper.sqlite" in paths
        for account in accounts:
            Store(account).audit()

        # Subscription ownership is distinct from the public-cache denial.
        assert registry.finish_owned(
            subscription, 1, "integration-producer", token=token
        )
    finally:
        client.close()
        producer.close()
        registry.release_leader("integration-producer")
