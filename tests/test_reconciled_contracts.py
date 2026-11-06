"""Regression contracts at the boundary between the two source branches."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from bcc.config import defaults, validate, poly_window_allows
from bcc.market import PublicData
from bcc.runtime_process import owned_service, service_command_matches, stop_owned_service


def test_archived_zero_window_keeps_every_second_until_cutoff():
    cfg = defaults('POLYMARKET')
    cfg['name'] = 'Reconciled timing contract'
    cfg['polymarket'].update(entry_signal_mode='before_close',
        entry_seconds_before_close=40, entry_window_seconds=0,
        entry_min_seconds_left=5)
    before = deepcopy(cfg)
    saved = validate(cfg)
    assert cfg == before
    rule = saved['polymarket']
    for second in range(100, 400):
        assert poly_window_allows(rule, 100, 400, second, second) == (360 <= second <= 395)


@pytest.mark.parametrize('key', ['entry_signal_mode', 'entry_seconds_before_close',
    'entry_window_seconds', 'entry_min_seconds_left'])
def test_partial_canonical_schedule_is_rejected_instead_of_guessed(key):
    cfg = defaults('POLYMARKET')
    cfg['name'] = 'Reconciled timing contract'
    cfg['polymarket'].pop(key)
    with pytest.raises(ValueError, match='обязательные параметры'):
        validate(cfg)


def test_urgent_existing_position_includes_public_margin_dependencies():
    source = PublicData()
    try:
        source._thread.position_mark = True
        root = 'https://api.bybit.com/v5/market/'
        assert source._position_critical_request(root + 'risk-limit', {'symbol': 'BTCUSDT'})
        assert source._position_critical_request(root + 'tickers', {'category': 'linear'})
        assert not source._position_critical_request(root + 'kline', {'symbol': 'BTCUSDT'})
        source._thread.position_mark = False
        assert not source._position_critical_request(root + 'risk-limit', {'symbol': 'BTCUSDT'})
    finally:
        source.close()


def test_service_command_requires_exact_module_and_unambiguous_root(tmp_path):
    args = ['python.exe', '-m', 'bcc.runtime_service', '--data-root', str(tmp_path)]
    assert service_command_matches(args, tmp_path)
    assert not service_command_matches(args + ['--data-root', str(tmp_path)], tmp_path)
    assert not service_command_matches(['python.exe', '-m', 'unrelated', *args[3:]], tmp_path)
    assert not service_command_matches(None, tmp_path)


def test_redirector_health_must_belong_to_owned_launcher(tmp_path, monkeypatch):
    root = ['python.exe', '-m', 'bcc.runtime_service', '--data-root', str(tmp_path)]
    process = SimpleNamespace(pid=200, create_time=lambda: 123.,
        status=lambda: 'running', cmdline=lambda: root,
        parents=lambda: [SimpleNamespace(pid=100)])
    monkeypatch.setattr('bcc.runtime_process.psutil.Process', lambda _: process)
    launcher = SimpleNamespace(pid=100, poll=lambda: None)
    health = dict(pid=200, created=123.)
    assert owned_service(launcher, tmp_path, health) is process
    process.parents = lambda: [SimpleNamespace(pid=999)]
    assert owned_service(launcher, tmp_path, health) is None


def test_stop_redirector_preserves_unrelated_descendants(tmp_path, monkeypatch):
    events = []
    args = ['python.exe', '-m', 'bcc.runtime_service', '--data-root', str(tmp_path)]
    service = SimpleNamespace(cmdline=lambda: args,
        terminate=lambda: events.append('service'), kill=lambda: events.append('kill-service'))
    unrelated = SimpleNamespace(cmdline=lambda: ['python.exe', '-m', 'unrelated'],
        terminate=lambda: pytest.fail('unrelated process terminated'))
    parent = SimpleNamespace(children=lambda recursive: [service, unrelated])
    monkeypatch.setattr('bcc.runtime_process.psutil.Process', lambda _: parent)
    monkeypatch.setattr('bcc.runtime_process.psutil.wait_procs', lambda xs, timeout: (xs, []))
    launcher = SimpleNamespace(pid=100, poll=lambda: None,
        terminate=lambda: events.append('launcher'), wait=lambda timeout: events.append('wait'),
        kill=lambda: pytest.fail('unneeded force stop'))
    stop_owned_service(launcher, tmp_path)
    assert events == ['service', 'launcher', 'wait']
