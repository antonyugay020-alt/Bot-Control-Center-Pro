"""Event-driven WS cache admission: lease, subscription and REST quotas."""
import time

from bcc.data_cache import PublicCache
from bcc.market_hub import HubRegistry


BOOK_URL = "https://api.bybit.com/v5/market/orderbook"
PARAMS = {"category": "linear", "symbol": "BTCUSDT", "limit": 50}
HOST = "api.bybit.com"
BOOK = {
    "_bcc_ws_book": True, "retCode": 0,
    "time": 1763000000000,
    "result": {"s": "BTCUSDT", "b": [["100", "1"]],
               "a": [["101", "1"]], "u": 1, "seq": 1},
}


def test_stream_push_commits_without_http_budget(tmp_path):
    cache = PublicCache(tmp_path / "ws.sqlite")
    registry = HubRegistry(cache.path)
    try:
        registry.subscribe(BOOK_URL, PARAMS)
        assert registry.claim_leader("my-hub", lifetime=60)
        token = registry.leader_token("my-hub")
        assert cache.publish_websocket(
            BOOK_URL, PARAMS, HOST, BOOK, ttl=.2,
            leader_owner="my-hub", leader_token=token)
        assert cache.read_fresh(BOOK_URL, PARAMS, HOST) == BOOK
        with cache.db() as db:
            assert db.execute(
                "SELECT count FROM request_budget WHERE host=?", (HOST,)
            ).fetchone() is None
    finally:
        cache.close()


def test_stale_producer_token_cannot_overwrite_live_book(tmp_path):
    cache = PublicCache(tmp_path / "ws.sqlite")
    registry = HubRegistry(cache.path)
    try:
        registry.subscribe(BOOK_URL, PARAMS)
        assert registry.claim_leader("first", lifetime=60)
        token = registry.leader_token("first")
        assert cache.publish_websocket(
            BOOK_URL, PARAMS, HOST, BOOK, ttl=.2,
            leader_owner="first", leader_token=token)
        registry.release_leader("first", token=token)
        assert registry.claim_leader("second", lifetime=60)
        assert not cache.publish_websocket(
            BOOK_URL, PARAMS, HOST, BOOK, ttl=.2,
            leader_owner="first", leader_token=token)
    finally:
        cache.close()


def test_no_subscriber_no_publication(tmp_path):
    cache = PublicCache(tmp_path / "ws.sqlite")
    registry = HubRegistry(cache.path)
    try:
        assert registry.claim_leader("my-hub", lifetime=60)
        token = registry.leader_token("my-hub")
        assert not cache.publish_websocket(
            BOOK_URL, PARAMS, HOST, BOOK, ttl=.2,
            leader_owner="my-hub", leader_token=token)
        assert cache.read_fresh(BOOK_URL, PARAMS, HOST) is None
    finally:
        cache.close()
