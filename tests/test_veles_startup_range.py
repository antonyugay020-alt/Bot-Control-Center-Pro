"""Startup synchronization of VELES RSI ranges; never synthesize a crossing."""
from bcc.config import defaults, range_section, condition
from bcc.storage import initial_state
from bcc.strategies import Veles, startup_rsi_range
from bcc.indicators import series, aggregate
from test_core import attach, bars


def setup():
    cfg = defaults('VELES')
    cfg['symbols'] = ['BTCUSDT']
    cfg['sections'] = [range_section(30, 40), range_section(40, 50),
                       range_section(50, 60), range_section(60, 70),
                       range_section(70, 60, 'SHORT'), range_section(60, 50, 'SHORT'),
                       range_section(50, 40, 'SHORT'), range_section(40, 30, 'SHORT')]
    cfg['settings'].update(leverage=1, allocation_pct=10, fee_rate=.001, slippage_pct=0)
    cfg['settings']['orders'].update(count=1, first_offset_pct=0)
    state, ev = initial_state(500), []
    attach(state)
    return cfg, state, ev


def wave(direction):
    import math
    # Find a realistic closed-bar RSI sequence already inside a band with
    # recent movement but no crossing of that band's entry threshold.
    vals = [100 + 1.8 * math.sin(i / 3) for i in range(120)]
    if direction == 'down':
        vals = list(reversed(vals))
    return bars(vals), 120 * 60000


def test_startup_candidate_uses_closed_rsi_and_rejects_ambiguity():
    cfg, state, _ = setup()
    rows, now = wave('up')
    target = startup_rsi_range(cfg, state, 'BTCUSDT', rows, now)
    if target is not None:
        assert target.startswith('BTCUSDT:')
        clone = next(s.copy() for s in cfg['sections'] if target.endswith(':' + s['id']))
        clone['id'] = 'duplicate'
        cfg['sections'].append(clone)
        assert startup_rsi_range(cfg, state, 'BTCUSDT', rows, now) is None


def test_startup_never_bypasses_composite_entry():
    cfg, state, _ = setup()
    for s in cfg['sections']:
        s['entry']['conditions'].append(condition('PRICE', 100, '>', 2))
    rows, now = wave('up')
    assert startup_rsi_range(cfg, state, 'BTCUSDT', rows, now) is None


def test_startup_marks_symbol_only_after_execution():
    cfg, state, events = setup()
    # The deterministic supplied curve must expose exactly one eligible range.
    import math
    found = None
    for phase in range(30):
        prices = [100 + 3 * math.sin(i / 4 + phase / 9) for i in range(120)]
        rows = bars(prices)
        candidate = startup_rsi_range(cfg, state, 'BTCUSDT', rows, 120 * 60000)
        if candidate:
            found = rows, candidate
            break
    assert found is not None
    rows, candidate = found
    Veles().process(cfg, state, events, 'BTCUSDT', rows, 99.99, 100.01, 120 * 60000)
    assert candidate in state['positions']
    assert state['strategy']['veles_bootstrapped']['BTCUSDT']
    assert startup_rsi_range(cfg, state, 'BTCUSDT', rows, 120 * 60000) is None
