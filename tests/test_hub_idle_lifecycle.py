"""A public hub cannot survive forever after the last worker disappears."""
import os
import subprocess
import sys

from bcc.market_hub import HubRegistry


def test_hub_exits_when_no_subscribers(tmp_path):
    cache = tmp_path / 'market.sqlite'
    proc = subprocess.Popen([sys.executable, '-m', 'tools.market_hub_service',
                             '--cache', str(cache), '--poll', '0.02',
                             '--idle-exit', '0.2'],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = proc.communicate(timeout=8)
        assert proc.returncode == 0, (out, err)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)


def test_registry_knows_when_subscriber_expires(tmp_path, monkeypatch):
    import time
    r = HubRegistry(tmp_path / 'market.sqlite')
    assert not r.has_active_subscriptions()
    now = time.time()
    monkeypatch.setattr(time, 'time', lambda: now)
    r.subscribe('https://api.bybit.com/v5/market/orderbook',
                {'symbol': 'BTCUSDT'}, lifetime=4)
    assert r.has_active_subscriptions()
    monkeypatch.setattr(time, 'time', lambda: now + 5)
    assert not r.has_active_subscriptions()
