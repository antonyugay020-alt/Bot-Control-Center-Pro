import threading
import unittest

from bcc.strategy_dispatch import DispatchBusy, StrategyDispatcher


class StrategyDispatcherTests(unittest.TestCase):
    def test_fleet_size_is_independent_of_thread_budget(self):
        # Workload sizes are test data, never a production fleet ceiling.
        for owner_count, threads in ((3, 1), (83, 4), (521, 19)):
            with self.subTest(owner_count=owner_count, threads=threads):
                with StrategyDispatcher(workers=threads) as hub:
                    futures = [hub.submit('bot-' + str(i), lambda n=i: n * 3)
                               for i in range(owner_count)]
                    for future in futures:
                        future.result(timeout=15)
                    results = hub.collect()
                    self.assertEqual({key: value for key, value, error in results},
                                     {'bot-' + str(i): i * 3 for i in range(owner_count)})
                    self.assertTrue(all(error is None for _, _, error in results))
                    self.assertEqual(len(futures), owner_count)

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
