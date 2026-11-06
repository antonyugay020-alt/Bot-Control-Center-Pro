"""Cross-process delivery: one producer, independent clients, no worker HTTP."""
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from bcc.data_cache import PublicCache
from bcc.market_hub import MarketDataHub, HubRegistry

URL = 'https://api.bybit.com/v5/market/orderbook'


def test_hub_fifteen_independent_processes_one_public_fetch(tmp_path):
    path = tmp_path / 'market.sqlite'
    class Producer:
        def __init__(self):
            self.shared = PublicCache(path)
            self.calls = 0
        def get(self, url, params=None):
            self.calls += 1
            key, owner, cached = self.shared.begin(url, params, 'api.bybit.com')
            if owner:
                self.shared.finish(key, owner, 'api.bybit.com', {'symbol': 'BTCUSDT', 'source': 'hub'}, ttl=10)
        def close(self):
            self.shared.close()

    producer = Producer()
    hub = MarketDataHub(path, producer)
    code = '''import sys
from bcc.market import PublicData
p=PublicData()
try:
    d=p.get('https://api.bybit.com/v5/market/orderbook', {'symbol':'BTCUSDT'})
    assert d == {'symbol':'BTCUSDT','source':'hub'}, d
finally:
    p.close()
'''
    env = {**os.environ, 'BCC_PUBLIC_CACHE': str(path), 'BCC_MARKET_HUB_MODE': 'client'}
    processes = [subprocess.Popen([sys.executable, '-c', code], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(15)]
    try:
        for _ in range(500):
            if hub.registry.due():
                break
            threading.Event().wait(.01)
        assert hub.registry.due(), 'No consumer registered subscription; children=' + repr([(p.poll(), p.stderr.read().decode(errors='replace') if p.poll() is not None else '') for p in processes])
        assert hub.run_once() and producer.calls == 1
        for child in processes:
            out, err = child.communicate(timeout=10)
            assert child.returncode == 0, err.decode(errors='replace')
    finally:
        for child in processes:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)
        hub.close()
