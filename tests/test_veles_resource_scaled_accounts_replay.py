"""Scale acceptance of independent PAPER state; deterministic replay, not live Bybit."""
import math
import os
import time
import pytest
from concurrent.futures import ThreadPoolExecutor

from bcc.config import defaults, range_section
from bcc.runtime import process_tick
from bcc.scheduler import MarketTick
from bcc.storage import Store
from bcc.strategies import Veles, startup_rsi_range
from test_core import bars, book


@pytest.mark.parametrize('count', [7, 61, 241])
def test_many_independent_veles_paper_accounts(tmp_path, count):
    cfg = defaults('VELES')
    cfg['symbols'] = ['BTCUSDT']
    cfg['sections'] = [range_section(30, 40), range_section(40, 50),
                       range_section(50, 60), range_section(60, 70),
                       range_section(70, 60, 'SHORT'), range_section(60, 50, 'SHORT'),
                       range_section(50, 40, 'SHORT'), range_section(40, 30, 'SHORT')]
    cfg['settings'].update(leverage=1, allocation_pct=10, fee_rate=.001, slippage_pct=0)
    cfg['settings']['orders'].update(count=1, first_offset_pct=0)
    now_ms = int(time.time() * 1000)
    end = (now_ms // 60000 - 1) * 60000
    (tmp_path / 'probe').mkdir()
    probe = Store(tmp_path / 'probe', 500)
    for phase in range(80):
        rows = bars([100 + 3 * math.sin(i / 4 + phase / 9) for i in range(180)],
                    base=end - 179 * 60000)
        if startup_rsi_range(cfg, probe.read(), 'BTCUSDT', rows, now_ms):
            break
    else:
        raise AssertionError('No unambiguous RSI startup fixture')
    # Each independent account receives a newly observed quote at its turn.
    # This is a parallel PAPER-path benchmark, NOT a historical broadcast of
    # one soon-to-expire book to a large sequential fleet.
    def run_one(idx):
        folder = tmp_path / f'bot_{idx:04d}'
        folder.mkdir()
        store = Store(folder, 500)
        # The benchmark may run across several candle boundaries on slow
        # machines. Every independent account must receive a fresh, closed
        # candle window as well as a fresh order book; otherwise later bots
        # are rejected for stale market data instead of testing accounting.
        closed_end = (int(time.time() * 1000) // 60000 - 1) * 60000
        offset = closed_end - rows[-1]['t']
        observed_rows = [dict(row, t=row['t'] + offset) for row in rows]
        process_tick(cfg, store, Veles(), MarketTick('BTCUSDT', observed_rows, book(100), [], {}))
        assert len(store.read()['positions']) == 1, idx
        assert [e['action'] for e in store.history()] == ['OPEN'], idx
        process_tick(cfg, store, Veles(), MarketTick('BTCUSDT', observed_rows, book(100), [], {}))
        assert [e['action'] for e in Store(folder).history()] == ['OPEN'], idx
        store.audit()
        return idx

    # Parallelism is derived from available CPU resources and actual work.
    # Test fixture sizes do not define fleet or thread limits.
    parallelism = min(count, max(1, (os.cpu_count() or 1) * 2))
    with ThreadPoolExecutor(max_workers=parallelism) as pool:
        assert len(list(pool.map(run_one, range(count)))) == count
