"""Exercise the V34 policies through the actual elected Hub and writer gate."""
from concurrent.futures import ThreadPoolExecutor
import time

from bcc.hub_demand import DemandClient
from bcc.public_writer import WriterGate
from test_v27_writer_gate import wait_for_waiters
from test_v32_central_demand import central, URL, HOST, params


def test_real_hub_receipts_report_reuse_and_rearm(central):
    path, registry, cache, token, server = central
    client = DemandClient(path)
    url = 'https://api.bybit.com/v5/market/kline'
    query = dict(category='linear',symbol='BTCUSDT',interval='1',limit=500)
    key = client.request_once(url,query,priority=True)
    for _ in range(10):
        assert client.request_once(url,query,priority=True)==key
    stats=server.stats()
    assert stats['committed']==11 and stats['written_rows']==stats['write_batches']==1
    assert stats['reused']==10
    with cache.db() as db:
        before = db.execute('SELECT subscription_token FROM hub_subscriptions WHERE k=?',(key,)).fetchone()[0]
        db.execute('UPDATE hub_subscriptions SET next_due=? WHERE k=?',(time.time()+3600,key))
    assert client.request_once(url,query,priority=True)==key
    with cache.read_db() as db:
        after,due=db.execute('SELECT subscription_token,next_due FROM hub_subscriptions WHERE k=?',(key,)).fetchone()
    assert after!=before and due==0
    assert server.stats()['write_batches']==2


def test_actual_writer_gate_prefers_urgent_with_bounded_burst():
    gate=WriterGate()
    order=[]
    def enter(name,urgent):
        with gate.prioritize(urgent):
            with gate:
                order.append(name)
    with ThreadPoolExecutor(max_workers=7) as pool:
        with gate:
            futures=[pool.submit(enter,'normal',False)]
            wait_for_waiters(gate,1)
            for i in range(6):
                futures.append(pool.submit(enter,i,True))
                wait_for_waiters(gate,i+2)
        for future in futures:
            future.result(timeout=5)
    assert order==[0,1,2,3,'normal',4,5]
    assert gate.stats()['priority_grants']==6
    assert gate.stats()['waiting']==0 and not gate.stats()['held']


def test_actual_ws_publication_uses_priority_gate(central):
    path,registry,cache,token,server=central
    client=DemandClient(path)
    client.subscribe(URL,params(1),lifetime=90)
    before=cache._guard.stats()['priority_grants']
    row=dict(url=URL,params=params(1),host=HOST,ttl=1.,
             value=dict(retCode=0,result=dict(s=params(1)['symbol']),_bcc_ws_book=True))
    assert cache.publish_websockets([row],leader_owner='owner',leader_token=token)==[True]
    assert cache._guard.stats()['priority_grants']==before+1
    assert cache._guard._process_lock is None
