from test_veles_startup_range import setup, wave
from bcc.strategies import startup_rsi_rejection, startup_rsi_range, Veles


def test_startup_reports_short_history_without_trade():
    cfg, state, events = setup()
    section = cfg['sections'][0]
    reason = startup_rsi_rejection(cfg, state, 'BTCUSDT', section, [], 0, None)
    assert 'Недостаточно закрытых RSI-свечей' in reason
    assert not events and not state['positions']


def test_startup_reports_ambiguous_band_without_trade():
    cfg, state, events = setup()
    rows, now = wave('up')
    target = startup_rsi_range(cfg, state, 'BTCUSDT', rows, now)
    if target:
        original = next(s for s in cfg['sections'] if target.endswith(':' + s['id']))
        duplicate = original.copy()
        duplicate['id'] = 'duplicated'
        cfg['sections'].append(duplicate)
        assert startup_rsi_range(cfg, state, 'BTCUSDT', rows, now) is None
        assert 'несколько подходящих' in startup_rsi_rejection(cfg, state, 'BTCUSDT', original, rows, now, None)
    assert not events and not state['positions']


def test_composite_signal_does_not_bypass_filter():
    cfg, state, events = setup()
    from bcc.config import condition
    sec = cfg['sections'][0]
    sec['entry']['conditions'].append(condition('PRICE', 100, '>', 2))
    rows, now = wave('up')
    assert 'составной' in startup_rsi_rejection(cfg, state, 'BTCUSDT', sec, rows, now, None)
    assert not events and not state['positions']
