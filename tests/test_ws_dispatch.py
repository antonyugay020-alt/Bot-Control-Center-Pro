"""Publisher handoff: venue fairness, bounded growth and no duplicate work."""
import unittest

from bcc.ws_dispatch import MarketEventQueue


class EventHandoffTests(unittest.TestCase):
    def test_critical_clob_precedes_trade_flood(self):
        queue = MarketEventQueue(capacity=128)
        for n in range(90):
            self.assertTrue(queue.notify("binance_trade", f"COIN{n}USDT"))
        self.assertTrue(queue.notify("polybook", "123"))
        self.assertEqual(queue.take(timeout=0), ("polybook", "123"))

    def test_low_priority_is_not_starved(self):
        queue = MarketEventQueue(capacity=128, urgent_burst=3)
        for n in range(10):
            queue.notify("depth50", f"COIN{n}USDT")
        queue.notify("binance_trade", "BTCUSDT")
        items = [queue.take(timeout=0) for _ in range(4)]
        self.assertEqual(items[3], ("binance_trade", "BTCUSDT"))

    def test_same_token_coalesces_to_latest_stream_read(self):
        queue = MarketEventQueue(capacity=16)
        for _ in range(1000):
            self.assertTrue(queue.notify("polybook", "123"))
        self.assertEqual(queue.stats()["pending"], 1)
        self.assertEqual(queue.take(timeout=0), ("polybook", "123"))
        self.assertTrue(queue.notify("polybook", "123"))
        self.assertEqual(queue.take(timeout=0), ("polybook", "123"))

    def test_bounded_overload_drops_without_hanging_receivers(self):
        queue = MarketEventQueue(capacity=16)
        for n in range(16):
            self.assertTrue(queue.notify("binance_trade", f"COIN{n}USDT"))
        self.assertFalse(queue.notify("binance_trade", "BTCUSDT"))
        self.assertEqual(queue.stats()["dropped_notifications"], 1)
        self.assertEqual(queue.stats()["pending"], 16)

    def test_unrecognized_events_never_enqueued(self):
        queue = MarketEventQueue()
        self.assertFalse(queue.notify("execute-order", "BTCUSDT"))
        self.assertFalse(queue.notify("polybook", ""))
        self.assertEqual(queue.stats()["pending"], 0)

    def test_no_work_returns_none(self):
        queue = MarketEventQueue()
        self.assertIsNone(queue.take(timeout=0))


if __name__ == "__main__":
    unittest.main()
