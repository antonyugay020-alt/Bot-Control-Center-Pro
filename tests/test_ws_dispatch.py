"""Publisher handoff: venue fairness, bounded growth and no duplicate work."""
import unittest

from bcc.ws_dispatch import MarketEventQueue, ReconnectBackoff


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
        self.assertFalse(queue.notify([], "BTCUSDT"))
        self.assertFalse(queue.notify("polybook", ""))
        self.assertEqual(queue.stats()["pending"], 0)

    def test_reconnect_backoff_is_capped_and_recovers(self):
        policy = ReconnectBackoff(base=1, maximum=30, healthy_after=30)
        delays = [policy.after_session(0.1) for _ in range(8)]
        self.assertEqual(delays, [1, 2, 4, 8, 16, 30, 30, 30])
        self.assertEqual(policy.after_session(60), 1)
        self.assertEqual(policy.after_session(0.1), 2)

    def test_reconnect_backoff_rejects_invalid_limits(self):
        with self.assertRaises(ValueError):
            ReconnectBackoff(base=0)
        with self.assertRaises(ValueError):
            ReconnectBackoff(base=31, maximum=30)

    def test_no_work_returns_none(self):
        queue = MarketEventQueue()
        self.assertIsNone(queue.take(timeout=0))


if __name__ == "__main__":
    unittest.main()


def test_shared_hub_capacity_tracks_large_active_inventory_without_silent_drop():
    # 12k symbols * ticker/depth = 24k active keys, even when thousands
    # are held positions. No arbitrary 8192-event default may truncate them.
    queue = MarketEventQueue()
    active = {('depth50', f'S{i}USDT') for i in range(12000)}
    active.update(('tickers', f'S{i}USDT') for i in range(12000))
    priority = {('depth50', f'S{i}USDT') for i in range(1000)}
    priority.update(('tickers', f'S{i}USDT') for i in range(1000))
    queue.configure(active, priority)
    assert queue.stats()['event_capacity'] == len(active)
    assert queue.stats()['active_over_capacity'] == 0
    for item in sorted(active):
        assert queue.notify(*item)
    assert queue.stats()['pending'] == len(active)
    assert queue.stats()['dropped_notifications'] == 0
    # Critical exposure messages are served before most discovery frames.
    first = [queue.take(timeout=0) for _ in range(25)]
    assert sum(item in priority for item in first) >= 23
    assert queue.stats()['pending'] == len(active) - 25


def test_shared_hub_capacity_shrinks_when_demand_retires():
    queue = MarketEventQueue()
    active = {('depth50', f'S{i}USDT') for i in range(15000)}
    queue.configure(active)
    assert queue.stats()['event_capacity'] == 15000
    assert all(queue.notify(*item) for item in active)
    retained = {('depth50', f'S{i}USDT') for i in range(50)}
    queue.configure(retained)
    stats = queue.stats()
    assert stats['event_capacity'] == 8192
    assert stats['pending'] == len(retained)
    assert stats['filtered_inactive'] == 15000-50


def test_explicit_small_queue_capacity_still_evicts_background_before_risk():
    queue = MarketEventQueue(capacity=16)
    scans = {('depth50', f'S{i}USDT') for i in range(16)}
    hot = ('tickers', 'HELDUSDT')
    queue.configure(scans | {hot}, {hot})
    assert queue.stats()['event_capacity'] == 16
    for item in scans: assert queue.notify(*item)
    assert queue.notify(*hot)
    assert queue.stats()['evicted_background'] == 1
    assert queue.take(timeout=0) == hot


def test_over_capacity_is_exposed_not_misreported_as_lossless():
    queue = MarketEventQueue()
    demand = {('tickers', f'S{i}USDT') for i in range(100010)}
    queue.configure(demand)
    status = queue.stats()
    assert status['event_capacity'] == 100000
    assert status['active_over_capacity'] == 10
