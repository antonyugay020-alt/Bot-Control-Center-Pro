"""Deterministic infrastructure saturation: 800 distinct accounting owners."""
import threading
from bcc.strategy_dispatch import StrategyDispatcher


def test_800_independent_dispatch_keys_and_failure_isolation():
    gate = threading.Event()
    def operation(index):
        gate.wait(10)
        if index in (117, 799):
            raise RuntimeError(f'isolated-{index}')
        return (index, index * 3)
    dispatch = StrategyDispatcher(workers=16, maximum_pending=800)
    try:
        futures = [dispatch.submit(f'bot-{i}', operation, i) for i in range(800)]
        assert len({id(f) for f in futures}) == 800
        gate.set()
        for future in futures:
            try:
                future.result(timeout=15)
            except RuntimeError:
                pass
        rows = dispatch.collect()
        assert len(rows) == 800
        by_key = {key: (result, error) for key, result, error in rows}
        assert len(by_key) == 800
        for i in range(800):
            result, error = by_key[f'bot-{i}']
            if i in (117, 799):
                assert result is None and str(error) == f'isolated-{i}'
            else:
                assert result == (i, i * 3) and error is None
    finally:
        gate.set()
        dispatch.close()
