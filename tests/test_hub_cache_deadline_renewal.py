"""Cached tier checks must schedule a real fetch at its actual TTL boundary."""
import time

import pytest

from bcc.market import PublicData
from bcc.market_hub import MarketDataHub

BASE='https://api.bybit.com/v5/market/'
PARAMS={'category':'linear','symbol':'BTCUSDT'}


def test_cached_risk_response_cannot_delay_renewal_past_its_actual_residence(tmp_path):
    source=PublicData();hub=MarketDataHub(tmp_path/'cache.sqlite',source=source)
    try:
        key,owner,_=source.shared.begin(BASE+'risk-limit',PARAMS,'api.bybit.com')
        source.shared.finish(key,owner,'api.bybit.com',{'retCode':0,'time':1},ttl=240)
        assert hub._refresh_delay(BASE+'risk-limit',PARAMS)==120
        with source.shared.db() as db:db.execute('UPDATE cache SET until=? WHERE k=?',(time.time()+7,key))
        assert 6.9<=hub._refresh_delay(BASE+'risk-limit',PARAMS)<=7.1
        with source.shared.db() as db:db.execute('UPDATE cache SET until=? WHERE k=?',(time.time()-.1,key))
        assert hub._refresh_delay(BASE+'risk-limit',PARAMS)==.05
        assert hub._refresh_delay(BASE+'risk-limit',PARAMS,failed=True)==2
    finally:hub.close()


@pytest.mark.parametrize('mode',['synchronous','parallel','nonblocking'])
def test_each_hub_mode_rechecks_at_real_expiry_and_then_obtains_new_metadata(tmp_path,monkeypatch,mode):
    source=PublicData();hub=MarketDataHub(tmp_path/'cache.sqlite',source=source,max_workers=1 if mode=='synchronous' else 2)
    calls=[];url=BASE+'risk-limit'
    key,owner,_=source.shared.begin(url,PARAMS,'api.bybit.com')
    old=int((time.time()-233)*1000)
    source.shared.finish(key,owner,'api.bybit.com',{'retCode':0,'time':old},ttl=7)
    hub.registry.subscribe(url,PARAMS,priority=True)
    def get(url,params=None):
        value=source.shared.read_fresh(url,params,'api.bybit.com')
        if value is None:
            calls.append(1);k,owner,_=source.shared.begin(url,params,'api.bybit.com')
            value={'retCode':0,'time':int(time.time()*1000)}
            assert source.shared.finish(k,owner,'api.bybit.com',value,ttl=240)
        return value
    monkeypatch.setattr(source,'get',get)
    def run():
        if mode!='nonblocking':return hub.run_once(1)
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            result=hub.poll_nonblocking()
            if result:return result
            time.sleep(.01)
        pytest.fail('Hub never acknowledged the renewal')
    try:
        assert run()==[(key,True)] and not calls
        with hub.registry._connect() as db:
            due=db.execute('SELECT next_due FROM hub_subscriptions WHERE k=?',(key,)).fetchone()[0]
        assert 6.7<due-time.time()<7.1
        assert source.shared.read_fresh(url,PARAMS,'api.bybit.com')['time']==old
        with source.shared.db() as db:db.execute('UPDATE cache SET until=0 WHERE k=?',(key,))
        with hub.registry._connect() as db:db.execute('UPDATE hub_subscriptions SET next_due=0 WHERE k=?',(key,))
        assert run()==[(key,True)] and calls==[1]
        assert source.shared.read_fresh(url,PARAMS,'api.bybit.com')['time']>old
    finally:hub.close()
