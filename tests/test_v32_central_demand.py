"""Bounded local demand transport: real SQLite and independent client processes."""
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from bcc.data_cache import CacheBusy, PublicCache
from bcc.hub_demand import DemandClient, DemandServer, FLAG, endpoint_path, _send, _receive
from bcc.market_hub import HubRegistry
from bcc.public_writer import writer_gate

URL = 'https://api.bybit.com/v5/market/orderbook'
HOST = 'api.bybit.com'
ROOT = Path(__file__).resolve().parents[1]


def params(i):
    return dict(category='linear', symbol=f'Q{i:03d}USDT', limit=50)


@pytest.fixture
def central(tmp_path, monkeypatch):
    monkeypatch.setenv(FLAG, '1')
    monkeypatch.delenv('BCC_MARKET_HUB_MODE', raising=False)
    path = tmp_path/'market.sqlite'
    registry = HubRegistry(path)
    cache = PublicCache(path)
    cache._ensure_schema()
    assert registry.claim_leader('owner', lifetime=120)
    token = registry.leader_token('owner')
    server = DemandServer(path, registry.prepare_subscription,
                          lambda rows: registry.commit_subscriptions(rows, owner='owner', token=token))
    server.start()
    try:
        yield path, registry, cache, token, server
    finally:
        server.close()
        cache.close()


def test_commit_receipt_and_leader_generation(central):
    path, registry, cache, token, server = central
    client = DemandClient(path)
    key = client.subscribe(URL, params(1), lifetime=90, priority=True)
    with cache.read_db() as db:
        row = db.execute('SELECT active_until,priority_until FROM hub_subscriptions WHERE k=?',(key,)).fetchone()
        assert row and row[0] > row[1] > time.time()
    assert registry.release_leader('owner', token=token)
    assert registry.claim_leader('successor', lifetime=120)
    with pytest.raises(OSError):
        client.subscribe(URL, params(2))
    with cache.read_db() as db:
        assert db.execute('SELECT count(*) FROM hub_subscriptions').fetchone()[0] == 1
    assert server.stats()['failed_batches'] == 1


@pytest.mark.parametrize('invalid', ['token', 'private_route', 'force_due'])
def test_invalid_demand_never_reaches_sqlite(central, invalid):
    path, registry, cache, token, server = central
    endpoint = json.loads(endpoint_path(path).read_text(encoding='utf-8'))
    frame = dict(token=endpoint['token'],request=dict(url=URL, params=params(1),
                  lifetime=60,priority=False,force_due=False))
    if invalid == 'token':
        frame['token'] = 'x'*64
    elif invalid == 'private_route':
        frame['request']['url'] = 'https://api.bybit.com/v5/order/create'
    else:
        frame['request']['force_due'] = True
    with socket.create_connection(('127.0.0.1',endpoint['port']),timeout=2) as sock:
        deadline = time.monotonic()+2.
        _send(sock,frame,deadline)
        assert _receive(sock,deadline) == {'error':'unavailable'}
    with cache.read_db() as db:
        assert db.execute('SELECT count(*) FROM hub_subscriptions').fetchone()[0] == 0
    assert server.stats()['received'] == 0


def test_client_cache_cannot_write_or_migrate(tmp_path, monkeypatch):
    monkeypatch.setenv(FLAG,'1')
    monkeypatch.setenv('BCC_MARKET_HUB_MODE','client')
    path = tmp_path/'absent.sqlite'
    cache = PublicCache(path)
    try:
        with pytest.raises(CacheBusy):
            cache._ensure_schema()
        with pytest.raises(CacheBusy):
            with cache.db():
                pytest.fail('client writer admitted')
        assert not path.exists()
        assert not list(tmp_path.glob('*.writer.lock*'))
    finally:
        cache.close()


def test_one_shot_rearm_and_stale_endpoint_recovery(central):
    path, registry, cache, token, server = central
    url = 'https://api.bybit.com/v5/market/kline'
    query = dict(category='linear',symbol='BTCUSDT',interval='1',limit=500)
    client = DemandClient(path)
    key = client.request_once(url,query)
    with cache.db() as db:
        before = db.execute('SELECT subscription_token FROM hub_subscriptions WHERE k=?',(key,)).fetchone()[0]
        db.execute('UPDATE hub_subscriptions SET next_due=? WHERE k=?',(time.time()+3600,key))
    assert client.request_once(url,query) == key
    with cache.read_db() as db:
        after, due = db.execute('SELECT subscription_token,next_due FROM hub_subscriptions WHERE k=?',(key,)).fetchone()
        assert after != before and due == 0
    endpoint = endpoint_path(path)
    original = endpoint.read_bytes()
    try:
        bad = json.loads(original)
        bad['token'] = '0'*64
        endpoint.write_text(json.dumps(bad),encoding='utf-8')
        with pytest.raises(OSError):
            client.request_once(url,query)
    finally:
        endpoint.write_bytes(original)
    assert client.request_once(url,query) == key


def test_publication_under_independent_process_demand_load(central):
    path, registry, cache, token, server = central
    client = DemandClient(path)
    # All symbols admitted before the independent high-rate producer starts.
    for i in range(59):
        client.subscribe(URL,params(i),lifetime=90,priority=True)
    assert cache._guard is registry._writer_gate
    assert cache._guard._process_lock is None
    code = '''import sys,time
from bcc.hub_demand import DemandClient
from bcc.data_cache import PublicCache
p,worker=sys.argv[1:]; worker=int(worker)
client=DemandClient(p); cache=PublicCache(p)
try:
    for i in range(120):
        n=(i*7+worker*13)%59
        query=dict(category='linear',symbol=f'Q{n:03d}USDT',limit=50)
        client.subscribe('https://api.bybit.com/v5/market/orderbook',query,lifetime=90,priority=bool(i%2))
        cache.read_fresh('https://api.bybit.com/v5/market/orderbook',query,'api.bybit.com')
    assert cache._connection is None
finally:cache.close()
'''
    env = dict(os.environ, PYTHONPATH=str(ROOT), BCC_MARKET_HUB_MODE='client')
    procs = []
    try:
        for i in range(3):
            procs.append(subprocess.Popen([sys.executable,'-c',code,str(path),str(i)],cwd=ROOT,env=env,
                                          stdout=subprocess.PIPE,stderr=subprocess.PIPE))
        # Simulated market messages, not exchange evidence. Exercise the
        # actual public cache publication transaction under demand churn.
        began = time.monotonic()
        for n in range(240):
            rows = [dict(url=URL,params=params(i),host=HOST,ttl=1.,
                     value=dict(retCode=0,result=dict(s=params(i)['symbol']),_bcc_ws_book=True))
                     for i in range(59)]
            assert all(cache.publish_websockets(rows,leader_owner='owner',leader_token=token))
            time.sleep(.005)
        elapsed = time.monotonic()-began
        for proc in procs:
            out,err=proc.communicate(timeout=30)
            if proc.returncode != 0:
                pytest.fail(err.decode('utf-8',errors='replace'),pytrace=False)
        stats=server.stats()
        assert stats['committed']==419 and not stats['failed_batches'] and not stats['rejected']
        assert stats['max_batch'] >= 2
        assert cache._guard.stats()['timeouts']==0
        assert cache.stats['local_busy']==cache.stats['sqlite_busy']==0
        assert not list(path.parent.glob('*.writer.lock*'))
        with cache.read_db() as db:
            assert db.execute('SELECT count(*) FROM hub_subscriptions').fetchone()[0]==59
            assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        print(f'PAPER synthetic load: 14160 rows, 240 commits, 419 demands, {elapsed:.3f}s; {stats}')
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=10)


def test_public_data_uses_committed_hub_receipt_not_local_registry(central, monkeypatch):
    from bcc.market import PublicData, DataUnavailable
    path, registry, cache, token, server = central
    monkeypatch.setenv('BCC_PUBLIC_CACHE',str(path))
    monkeypatch.setenv('BCC_MARKET_HUB_MODE','client')
    source = PublicData()
    key = HubRegistry.identity(URL, params(1))[0]
    endpoint = endpoint_path(path)
    original = endpoint.read_bytes()
    def demand():
        source._renew_hub_demand(lambda p: pytest.fail('client registry writer'), key, URL,params(1),
                                 lifetime=90,renewal=30,priority=True,priority_renewal=30)
    try:
        bad = json.loads(original)
        bad['token'] = '0'*64
        endpoint.write_text(json.dumps(bad),encoding='utf-8')
        with pytest.raises(DataUnavailable):
            demand()
        assert not source._hub_renew_at and not source._hub_priority_until
        endpoint.write_bytes(original)
        demand()
        assert source._hub_renew_at[key] > time.monotonic()
        assert source._hub_priority_until[key] > time.monotonic()
        with cache.read_db() as db:
            assert db.execute('SELECT count(*) FROM hub_subscriptions WHERE k=?',(key,)).fetchone()[0]==1
        assert source.shared._connection is None
    finally:
        endpoint.write_bytes(original)
        source.close()


def test_read_only_client_preserves_provider_denial(central, monkeypatch):
    path, registry, cache, token, server = central
    with cache.db() as db:
        db.execute('INSERT INTO provider_denial(host,status,until_mono,recorded_mono,until_utc,boot_epoch) VALUES(?,?,?,?,?,?)',
                   (HOST,403,time.monotonic()+60,time.monotonic(),time.time()+60,cache._boot_epoch()))
    monkeypatch.setenv('BCC_MARKET_HUB_MODE','client')
    reader = PublicCache(path)
    try:
        assert reader.denied_status(HOST)==403
        assert reader._connection is None
    finally:
        reader.close()
