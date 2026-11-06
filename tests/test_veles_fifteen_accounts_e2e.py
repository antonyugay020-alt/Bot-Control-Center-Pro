"""15 isolated VELES PAPER accounts: closed RSI bars => actual persisted OPEN.

Offline deterministic market replay; not a claim of live Bybit execution.
"""
import math

from bcc.config import defaults, range_section
from bcc.storage import Store, initial_state
from bcc.strategies import Veles, startup_rsi_range
from test_core import attach, bars


def test_fifteen_independent_rsi_bots_commit_real_sqlite_opens(tmp_path):
    configs = []
    for index in range(15):
        cfg = defaults('VELES')
        cfg['symbols'] = ['BTCUSDT']
        cfg['sections'] = [range_section(30, 40), range_section(40, 50),
                           range_section(50, 60), range_section(60, 70),
                           range_section(70, 60, 'SHORT'), range_section(60, 50, 'SHORT'),
                           range_section(50, 40, 'SHORT'), range_section(40, 30, 'SHORT')]
        cfg['settings'].update(leverage=1, allocation_pct=10, fee_rate=.001, slippage_pct=0)
        cfg['settings']['orders'].update(count=1, first_offset_pct=0)
        configs.append(cfg)

    # A deterministic replay of prices with enough bars for RSI and an
    # unambiguous startup range. Use no mocked strategy signal/entry path.
    found = None
    for phase in range(80):
        rows = bars([100 + 3 * math.sin(i / 4 + phase / 9) for i in range(180)])
        now = 180 * 60000
        for cfg in configs[:1]:
            scratch = initial_state(500)
            candidate = startup_rsi_range(cfg, scratch, 'BTCUSDT', rows, now)
            if candidate:
                found = rows, now
                break
        if found:
            break
    assert found, 'Cannot construct valid closed-bar RSI entry fixture'
    rows, now = found

    for index, cfg in enumerate(configs):
        folder = tmp_path / f'bot_{index:02d}'
        folder.mkdir()
        store = Store(folder, 500)
        with store.transaction() as (state, events):
            attach(state)
            Veles().process(cfg, state, events, 'BTCUSDT', rows, 99.99, 100.01, now)
            assert [e['action'] for e in events] == ['OPEN'], (index, events)
            assert len(state['positions']) == 1, index
            state.pop('_books', None)
        store.audit()
        reopened = Store(folder).read()
        assert len(reopened['positions']) == 1
        assert [e['action'] for e in store.history()] == ['OPEN']
        with Store(folder).transaction() as (state, events):
            attach(state)
            Veles().process(cfg, state, events, 'BTCUSDT', rows, 99.99, 100.01, now)
            assert not any(e['action'] == 'OPEN' for e in events), index
            state.pop('_books', None)
        Store(folder).audit()


def test_runtime_market_tick_commits_paper_entry(tmp_path):
    """Real process_tick (quotes, risk checks, SQLite commit), offline replay."""
    import time
    from bcc.runtime import process_tick
    from bcc.scheduler import MarketTick
    from test_core import book

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
    folder = tmp_path / 'runtime'
    folder.mkdir()
    store = Store(folder, 500)
    for phase in range(80):
        prices = [100 + 3 * math.sin(i / 4 + phase / 9) for i in range(180)]
        rows = bars(prices, base=end - 179 * 60000)
        if startup_rsi_range(cfg, store.read(), 'BTCUSDT', rows, now_ms):
            break
    else:
        assert False, 'No deterministic RSI band matched'
    tick = MarketTick('BTCUSDT', rows, book(100), [], {})
    process_tick(cfg, store, Veles(), tick)
    store.audit()
    assert len(Store(folder).read()['positions']) == 1
    assert [e['action'] for e in store.history()] == ['OPEN']
    process_tick(cfg, store, Veles(), tick)
    store.audit()
    assert [e['action'] for e in store.history()] == ['OPEN']
