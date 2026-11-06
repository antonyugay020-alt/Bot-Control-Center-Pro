import threading
import time
import unittest

from bcc.strategy_dispatch import DispatchBusy, StrategyDispatcher


class StrategyDispatcherTests(unittest.TestCase):
    def test_800_keys_no_loss(self):
        with StrategyDispatcher(workers=12, maximum_pending=800) as hub:
            futures = [hub.submit('bot-' + str(i), lambda n=i: n * 3) for i in range(800)]
            results = hub.collect()
            while len(results) < 800:
                time.sleep(.001)
                results.extend(hub.collect())
            self.assertEqual({key: value for key, value, error in results},
                             {'bot-' + str(i): i * 3 for i in range(800)})
            self.assertTrue(all(error is None for _, _, error in results))
            self.assertEqual(len(futures), 800)

    def test_bounded_and_no_overlap(self):
        gate = threading.Event()
        with StrategyDispatcher(workers=1, maximum_pending=2) as hub:
            a = hub.submit('a', lambda: gate.wait(2))
            hub.submit('b', lambda: 2)
            with self.assertRaises(DispatchBusy):
                hub.submit('c', lambda: 3)
            with self.assertRaises(DispatchBusy):
                hub.submit('a', lambda: 4)
            gate.set()
            self.assertTrue(a.result(timeout=2))

    def test_errors_are_isolated(self):
        def broken():
            raise RuntimeError('intentional')
        with StrategyDispatcher(workers=2, maximum_pending=2) as hub:
            hub.submit('bad', broken)
            good = hub.submit('good', lambda: 42)
            good.result(timeout=2)
            outcomes = hub.collect()
            self.assertEqual(len(outcomes), 2)
            self.assertEqual([x[1] for x in outcomes if x[0] == 'good'], [42])
            self.assertIsInstance(next(x[2] for x in outcomes if x[0] == 'bad'), RuntimeError)

    def test_closed_denies_new_execution(self):
        hub = StrategyDispatcher()
        hub.close()
        with self.assertRaises(DispatchBusy):
            hub.submit('a', lambda: 1)
