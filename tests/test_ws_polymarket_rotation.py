"""Rotating Polymarket market tokens must not accumulate obsolete WS books."""
import json
import time

from bcc.ws_polymarket import PolymarketMarketWS
from bcc.ws_quotes import StreamUnavailable
from bcc.market import PublicData
from bcc.market_hub import MarketDataHub


class Socket:
    def __init__(self):
        self.sent = []

    def send(self, payload):
        self.sent.append(json.loads(payload))

    def close(self):
        pass


def test_token_rotation_unsubscribes_expired_assets():
    stream = PolymarketMarketWS()
    socket = Socket()
    epoch = stream.connect(socket)
    stream.sync_assets(["123", "456"])
    assert socket.sent[0] == {
        "assets_ids": ["123", "456"], "operation": "subscribe"}
    stamp = str(int(time.time() * 1000))
    assert stream.ingest({
        "event_type": "book", "asset_id": "123", "timestamp": stamp,
        "bids": [{"price": "0.4", "size": "5"}],
        "asks": [{"price": "0.5", "size": "10"}],
    }, generation=epoch)
    stream.sync_assets(["456", "789"])
    assert socket.sent[-2] == {
        "assets_ids": ["123"], "operation": "unsubscribe"}
    assert socket.sent[-1] == {
        "assets_ids": ["789"], "operation": "subscribe"}
    try:
        stream.get_book("123")
    except StreamUnavailable:
        pass
    else:
        raise AssertionError("expired market book was reused")


class FakeMarketStream:
    def __init__(self):
        self.assets = []

    def sync_assets(self, assets):
        self.assets = list(assets)

    def get_book(self, asset):
        return {
            "asset_id": asset, "timestamp": str(int(time.time() * 1000)),
            "bids": [{"price": "0.42", "size": "10"}],
            "asks": [{"price": "0.46", "size": "10"}],
        }


def test_fourteen_up_down_tokens_use_single_shared_hub(tmp_path):
    stream = FakeMarketStream()
    source = PublicData(polymarket_stream=stream)
    hub = MarketDataHub(tmp_path / "poly.sqlite", source=source)
    try:
        tokens = [str(i + 1000) for i in range(14)]
        for asset in tokens:
            hub.registry.subscribe("https://clob.polymarket.com/book",
                                   {"token_id": asset})
        assert hub.acquire_leadership()
        hub.subscribe_active_websockets()
        assert stream.assets == sorted(tokens)
        for token in tokens:
            assert hub.publish_ws_event("polybook", token)
            data = source.shared.read_fresh(
                "https://clob.polymarket.com/book", {"token_id": token},
                "clob.polymarket.com")
            assert data["_bcc_ws_polybook"] is True
            assert data["asset_id"] == token
        with source.shared.db() as db:
            assert db.execute(
                "SELECT count FROM request_budget WHERE host='clob.polymarket.com'"
            ).fetchone() is None
    finally:
        hub.close()
