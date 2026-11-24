"""Real SQLite registry and independent bounded producer execution."""
import sqlite3
import threading
import time
from bcc.market import PublicData
from bcc.market_hub import MarketDataHub

BASE='https://api.bybit.com/v5/market/'
KLINE=BASE+'kline'
RISK=BASE+'risk-limit'
INSTRUMENT=BASE+'instruments-info'


def test_history_and_metadata_progress_independently(tmp_path, monkeypatch):
    source=PublicData()
    hub=MarketDataHub(tmp_path/'cache.sqlite',source=source,max_workers=6)
    hub.enable_split_history()
    release=threading.Event()
    lock=threading.Lock()
    started=set()
    risk=set()

    def get(url,params=None):
        symbol=params['symbol']
        if url==KLINE:
            with lock:started.add(symbol)
            assert release.wait(12)
        else:
            with lock:risk.add(symbol)
        return {'retCode':0}

    monkeypatch.setattr(source,'get',get)
    try:
        for i in range(20):
            hub.registry.subscribe(KLINE,dict(category='linear',symbol=f'H{i}USDT',
                interval='1',limit=100),priority=i<19)
            hub.registry.subscribe(RISK,dict(category='linear',symbol=f'R{i}USDT'),priority=True)
        deadline=time.monotonic()+6
        while time.monotonic()<deadline:
            hub.poll_nonblocking()
            with lock:
                if len(started)==3 and len(risk)>=6:break
            time.sleep(.01)
        with lock:
            assert len(started)==3 and 'H19USDT' in started
            assert len(risk)>=6
        assert len(hub._inflight)<=6
    finally:
        release.set()
        hub.close()


def test_metadata_cache_hits_do_not_take_writer_or_repeat_each_second(tmp_path, monkeypatch):
    source=PublicData()
    hub=MarketDataHub(tmp_path/'cache.sqlite',source=source,max_workers=6)
    hub.enable_split_history()
    params={'category':'linear','symbol':'BTCUSDT'}
    try:
        key,owner,_=source.shared.begin(INSTRUMENT,params,'api.bybit.com')
        assert source.shared.finish(key,owner,'api.bybit.com',{'retCode':0},ttl=3600)
        assert 3595<=hub._refresh_delay(INSTRUMENT,params)<=3600
        def forbidden():
            raise AssertionError('Cache hit acquired the WS writer guard')
        monkeypatch.setattr(source.shared,'db',forbidden)
        _,owner,value=source.shared.begin(INSTRUMENT,params,'api.bybit.com')
        assert owner is None and value=={'retCode':0}
    finally:
        hub.close()


def test_failed_sql_ack_retains_finished_future_until_retry(tmp_path, monkeypatch):
    source=PublicData()
    hub=MarketDataHub(tmp_path/'cache.sqlite',source=source,max_workers=6)
    hub.enable_split_history()
    calls=[]
    params={'category':'linear','symbol':'BTCUSDT','interval':'1','limit':100}
    key=hub.registry.subscribe(KLINE,params)
    monkeypatch.setattr(source,'get',lambda *a,**k:calls.append(1))
    real=hub._acknowledge
    def fail(*args,**kwargs):
        raise sqlite3.OperationalError('database is locked')
    try:
        hub.poll_nonblocking()
        hub._inflight[key][0].result(timeout=2)
        monkeypatch.setattr(hub,'_acknowledge',fail)
        try:
            hub.poll_nonblocking()
        except sqlite3.OperationalError:
            pass
        else:
            raise AssertionError('Synthetic lock was not exercised')
        assert key in hub._inflight and calls==[1]
        monkeypatch.setattr(hub,'_acknowledge',real)
        assert (key,True) in hub.poll_nonblocking()
        assert key not in hub._inflight and calls==[1]
    finally:
        hub.close()
