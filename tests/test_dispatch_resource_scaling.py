"""Cross-owner isolation under varied fleet sizes and adjustable thread budgets."""
import threading
import pytest
from bcc.strategy_dispatch import StrategyDispatcher

@pytest.mark.parametrize("owner_count,threads", [(7, 1), (73, 3), (521, 19)])
def test_independent_dispatch_keys_and_failure_isolation(owner_count, threads):
    gate = threading.Event()
    failures = {owner_count // 3, owner_count - 1}
    def operation(index):
        assert gate.wait(10)
        if index in failures:
            raise RuntimeError(f'isolated-{index}')
        return index, index * 3
    dispatch = StrategyDispatcher(workers=threads)
    try:
        futures = [dispatch.submit(f'bot-{i}', operation, i) for i in range(owner_count)]
        assert len({id(f) for f in futures}) == owner_count
        gate.set()
        for future in futures:
            try:
                future.result(timeout=15)
            except RuntimeError:
                pass
        rows = dispatch.collect()
        assert len(rows) == owner_count
        by_key = {key: (result, error) for key, result, error in rows}
        assert len(by_key) == owner_count
        for i in range(owner_count):
            result, error = by_key[f'bot-{i}']
            if i in failures:
                assert result is None and str(error) == f'isolated-{i}'
            else:
                assert result == (i, i * 3) and error is None
    finally:
        gate.set()
        dispatch.close()
