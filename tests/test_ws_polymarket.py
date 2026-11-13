"""Offline Polymarket CLOB market stream safety tests. No network or API keys."""
import json
import unittest

from bcc.ws_polymarket import PolymarketMarketWS
from bcc.ws_quotes import StreamUnavailable


class Clock:
    def __init__(self):
        self.wall = 1763000000.0
        self.mono = 100.0


class MarketChannelTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.events = []
        self.stream = PolymarketMarketWS(
            wall=lambda: self.clock.wall, monotonic=lambda: self.clock.mono,
            on_update=lambda *event: self.events.append(event),
        )
        self.stream.subscribe(["123", "456"])
        self.generation = self.stream.connect()

    def book(self, token="123", **extra):
        event = {
            "event_type": "book", "asset_id": token,
            "timestamp": str(int(self.clock.wall * 1000)),
            "bids": [{"price": "0.42", "size": "10"},
                     {"price": "0.4", "size": "20"}],
            "asks": [{"price": "0.46", "size": "15"},
                     {"price": "0.48", "size": "9"}],
        }
        event.update(extra)
        return event

    def change(self, **extra):
        event = {
            "event_type": "price_change",
            "timestamp": str(int(self.clock.wall * 1000)),
            "price_changes": [
                {"asset_id": "123", "price": "0.46", "size": "0",
                 "side": "SELL", "best_bid": "0.42", "best_ask": "0.48"},
            ],
        }
        event.update(extra)
        return event

    def test_snapshot_and_ask(self):
        self.assertTrue(self.stream.ingest(self.book(), generation=self.generation))
        data = self.stream.get_book("123")
        self.assertEqual(data["asks"][0]["price"], "0.46")
        self.assertEqual(data["bids"][0]["price"], "0.42")

    def test_legacy_buys_sells_snapshot(self):
        snapshot = self.book()
        snapshot["buys"] = snapshot.pop("bids")
        snapshot["sells"] = snapshot.pop("asks")
        self.assertTrue(self.stream.ingest(snapshot, generation=self.generation))
        self.assertEqual(self.stream.get_book("123")["asks"][0]["price"], "0.46")

    def test_mixed_book_field_variants_rejected(self):
        mixed = self.book()
        mixed["buys"] = mixed["bids"]
        self.assertFalse(self.stream.ingest(mixed, generation=self.generation))
        with self.assertRaises(StreamUnavailable):
            self.stream.get_book("123")

    def test_incomplete_legacy_snapshot_rejected(self):
        missing = self.book()
        missing["buys"] = missing.pop("bids")
        self.assertFalse(self.stream.ingest(missing, generation=self.generation))
        with self.assertRaises(StreamUnavailable):
            self.stream.get_book("123")

    def test_incremental_ask_removal(self):
        self.stream.ingest(self.book(), generation=self.generation)
        self.assertTrue(self.stream.ingest(self.change(), generation=self.generation))
        self.assertEqual(self.stream.get_book("123")["asks"][0]["price"], "0.48")

    def test_batch_handles_every_asset(self):
        two = [self.book("123"), self.book("456")]
        self.assertTrue(self.stream.ingest_message(json.dumps(two),
                          generation=self.generation))
        self.assertEqual(set(token for _, token in self.events), {"123", "456"})

    def test_delta_without_snapshot_denied(self):
        self.assertFalse(self.stream.ingest(self.change(), generation=self.generation))
        with self.assertRaises(StreamUnavailable):
            self.stream.get_book("123")

    def test_crossed_book_never_admitted(self):
        self.assertFalse(self.stream.ingest(
            self.book(asks=[{"price": "0.4", "size": "20"}]),
            generation=self.generation))
        with self.assertRaises(StreamUnavailable):
            self.stream.get_book("123")

    def test_corrupt_delta_invalidates_book(self):
        self.stream.ingest(self.book(), generation=self.generation)
        self.assertFalse(self.stream.ingest(
            self.change(price_changes=[
                {"asset_id": "123", "price": "NaN", "size": "4", "side": "BUY"}
            ]), generation=self.generation))
        with self.assertRaises(StreamUnavailable):
            self.stream.get_book("123")

    def test_disconnect_invalidates_all(self):
        self.stream.ingest(self.book(), generation=self.generation)
        self.stream.disconnect()
        with self.assertRaises(StreamUnavailable):
            self.stream.get_book("123")
        old = self.generation
        self.generation = self.stream.connect()
        self.assertFalse(self.stream.ingest(self.book(), generation=old))
        self.assertTrue(self.stream.ingest(self.book(), generation=self.generation))

    def test_age_expiry(self):
        self.stream.ingest(self.book(), generation=self.generation)
        self.clock.mono += 2.1
        with self.assertRaises(StreamUnavailable):
            self.stream.get_book("123")

    def test_unknown_token_rejected(self):
        self.assertFalse(self.stream.ingest(self.book("789"), generation=self.generation))

    def test_future_and_old_timestamps_denied(self):
        self.assertFalse(self.stream.ingest(self.book(timestamp="1"), generation=self.generation))
        self.assertFalse(self.stream.ingest(self.book(
            timestamp=str(int((self.clock.wall + 5) * 1000))), generation=self.generation))

    def test_malformed_unhashable_asset_never_crashes_receiver(self):
        self.assertFalse(self.stream.ingest(
            self.book(asset_id=["invalid"]), generation=self.generation))
        self.assertFalse(self.stream.ingest(
            self.change(price_changes=[{
                "asset_id": ["invalid"], "price": "0.46",
                "size": "2", "side": "SELL",
            }]), generation=self.generation))

    def test_non_numeric_public_token_denied(self):
        with self.assertRaises(ValueError):
            self.stream.subscribe("Bearer secret")

    def test_bad_bid_ask_comparison_rejects_delta(self):
        self.stream.ingest(self.book(), generation=self.generation)
        self.assertFalse(self.stream.ingest(
            self.change(price_changes=[{
                "asset_id": "123", "price": "0.43", "size": "9",
                "side": "BUY", "best_bid": "0.42",
            }]), generation=self.generation))
        with self.assertRaises(StreamUnavailable):
            self.stream.get_book("123")


if __name__ == "__main__":
    unittest.main()
