import multiprocessing
from bcc.market_hub import HubRegistry, MarketDataHub
from bcc.data_cache import PublicCache

URL = 'https://api.bybit.com/v5/market/orderbook'


def test_single_owner_and_release(tmp_path):
    path = tmp_path / 'shared.sqlite'
    a, b = HubRegistry(path), HubRegistry(path)
    assert a.claim_leader('one')
    assert not b.claim_leader('two')
    assert a.claim_leader('one')
    a.release_leader('one', token=a.leader_token('one'))
    assert b.claim_leader('two')
    b.release_leader('two', token=b.leader_token('two'))


def test_producers_do_not_double_fetch(tmp_path):
    path = tmp_path / 'shared.sqlite'
    calls = []
    class Source:
        def __init__(self):
            self.shared = PublicCache(path)
        def get(self, url, params=None):
            calls.append(url)
            k, owner, _ = self.shared.begin(url, params, 'api.bybit.com')
            if owner:
                self.shared.finish(k, owner, 'api.bybit.com', {'ok': 1}, ttl=10)
        def close(self):
            self.shared.close()
    registry = HubRegistry(path)
    registry.subscribe(URL, {'symbol': 'BTCUSDT'})
    x, y = MarketDataHub(path, Source()), MarketDataHub(path, Source())
    try:
        assert len(x.run_once()) == 1
        assert y.run_once() == []
        assert len(calls) == 1
    finally:
        x.close()
        y.close()


def _claim(path, owner, barrier, result):
    reg = HubRegistry(path)
    barrier.wait()
    result.put(reg.claim_leader(owner))


def test_concurrent_processes_single_leader(tmp_path):
    path = tmp_path / 'hub.sqlite'
    HubRegistry(path)
    ctx = multiprocessing.get_context('spawn')
    barrier = ctx.Barrier(5)
    result = ctx.Queue()
    processes = [ctx.Process(target=_claim, args=(path, str(i), barrier, result)) for i in range(5)]
    for p in processes: p.start()
    for p in processes:
        p.join(timeout=15)
        assert p.exitcode == 0
    assert [result.get(timeout=2) for _ in processes].count(True) == 1


def test_lost_lease_cannot_renew(tmp_path):
    registry = HubRegistry(tmp_path / 'hub.sqlite')
    assert registry.claim_leader('old')
    old_token = registry.leader_token('old')
    assert registry.renew_leader('old', token=old_token)
    registry.release_leader('old', token=old_token)
    assert registry.claim_leader('new')
    assert not registry.renew_leader('old', token=old_token)

def test_hub_stops_after_lease_transfer(tmp_path):
    path = tmp_path / 'hub.sqlite'
    registry = HubRegistry(path)
    registry.subscribe(URL, {'symbol':'BTCUSDT'})
    registry.subscribe(URL, {'symbol':'ETHUSDT'})
    calls=[]
    class Source:
        shared=object()
        def get(self,url,params=None):
            calls.append(params['symbol'])
            registry.release_leader(hub.owner, token=registry.leader_token(hub.owner))
            assert registry.claim_leader('new')
        def close(self): pass
    hub=MarketDataHub(path,Source())
    assert len(hub.run_once())==0  # stale owner cannot acknowledge completion
    assert len(calls)==1


def test_slow_request_keeps_leadership(tmp_path):
    import threading
    import time
    path = tmp_path / 'hub.sqlite'
    reg = HubRegistry(path)
    reg.subscribe(URL, {'symbol': 'BTCUSDT'})
    entered = threading.Event()
    finish = threading.Event()
    class SlowSource:
        shared = object()
        def get(self, url, params=None):
            entered.set()
            assert finish.wait(3)
        def close(self): pass
    hub = MarketDataHub(path, SlowSource())
    hub.lease_renew_interval = .02
    renewals = []
    original = hub.registry.renew_leader
    def renew(owner, **kwargs):
        renewals.append(owner)
        return original(owner, **kwargs)
    hub.registry.renew_leader = renew
    result = []
    worker = threading.Thread(target=lambda: result.extend(hub.run_once()))
    worker.start()
    assert entered.wait(2)
    time.sleep(.10)
    assert len(renewals) >= 2
    assert not reg.claim_leader('competitor')
    finish.set()
    worker.join(3)
    assert not worker.is_alive()
    assert len(result) == 1 and result[0][1]
    hub.close()
