"""Hub producer progress across subscriber incarnation replacement.

A slow HTTP result from a retired subscriber must never acknowledge its
successor, but must also not abort unrelated requests or revoke the valid
leader. These are offline PAPER-only checks with synthetic sources.
"""
import threading
import time

import pytest

from bcc.market_hub import HubRegistry, MarketDataHub


URL = "https://api.bybit.com/v5/market/orderbook"
STALE = {"category": "linear", "symbol": "BTCUSDT"}
OTHER = {"category": "linear", "symbol": "ETHUSDT"}


@pytest.mark.parametrize("mode", ("synchronous", "parallel", "nonblocking"))
def test_reincarnated_subscriber_does_not_starve_other_markets(tmp_path, mode):
    path = tmp_path / "hub.sqlite"
    registry = HubRegistry(path)
    stale_key = registry.subscribe(URL, STALE)
    other_key = registry.subscribe(URL, OTHER)
    # Ensure the old incarnation is dequeued before the unrelated request.
    with registry._connect() as db:
        db.execute(
            "UPDATE hub_subscriptions SET next_due=-10 WHERE k=?",
            (stale_key,),
        )

    stale_ack_attempted = threading.Event()

    class Source:
        shared = object()
        invalidated = False

        def get(self, url, params=None):
            if params["symbol"] == STALE["symbol"]:
                if not self.invalidated:
                    self.invalidated = True
                    # Model an expired subscriber renewing during HTTP.
                    with registry._connect() as db:
                        db.execute(
                            "UPDATE hub_subscriptions SET active_until=? WHERE k=?",
                            (time.time() - 1, stale_key),
                        )
                    registry.subscribe(URL, STALE)
            else:
                # A separate market cannot complete until the producer has
                # attempted to acknowledge the stale first incarnation.
                assert stale_ack_attempted.wait(5), "stale ack was not reached"

        def close(self):
            pass

    hub = MarketDataHub(
        path, Source(), max_workers=1 if mode == "synchronous" else 2
    )
    original_finish = hub.registry.finish_owned

    def observe_finish(key, *args, **kwargs):
        result = original_finish(key, *args, **kwargs)
        if key == stale_key:
            stale_ack_attempted.set()
        return result

    hub.registry.finish_owned = observe_finish
    try:
        if mode == "nonblocking":
            completed = []
            deadline = time.monotonic() + 5
            saw_stale_ack = False
            while time.monotonic() < deadline:
                completed.extend(hub.poll_nonblocking())
                if stale_ack_attempted.is_set() and not saw_stale_ack:
                    # A stale subscriber is not a stale Hub leader.
                    assert hub._leader_held
                    saw_stale_ack = True
                if (other_key, True) in completed:
                    break
                time.sleep(.01)
            assert saw_stale_ack
        else:
            completed = hub.run_once(limit=2)
            # Only the replacement incarnation may acknowledge this market.
            assert (stale_key, True) not in completed
            assert any(row[0] == stale_key for row in registry.due())

        assert (other_key, True) in completed
        assert hub.registry.leader_token(hub.owner) == hub._leader_token
    finally:
        stale_ack_attempted.set()
        hub.close()
