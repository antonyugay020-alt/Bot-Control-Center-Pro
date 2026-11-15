"""Risk tiers renew independently of price/candle admission and retain clocks."""
from copy import deepcopy
import threading
import time

import pytest

from bcc import market_clock
from bcc.market import PublicData, DataUnavailable
from bcc.margin import attach_snapshot, margin_status
from bcc.runtime import run_loop, process_tick
from bcc.scheduler import MarketTick
from bcc.storage import Store
from test_worker_observation_owner import account, NeverTrades
from test_ws_portfolio_observations import portfolio


def test_real_owner_renews_twenty_expired_tiers_while_quotes_and_history_are_blocked(portfolio,tmp_path,monkeypatch):
    symbols,reader,hub,stream=portfolio
    cfg,store,folder=account(tmp_path/'accounts',symbols);cfg['symbols']=symbols+[f'SCAN{i}USDT' for i in range(801)]
    with store.transaction(False) as (state,_):
        for row in state['margin_data'].values():row['risk_at']=time.time()-350
    templates=deepcopy(store.read(False)['margin_data'])
    wall,mono=time.time(),time.monotonic();market_clock.clock._reference=(wall,wall+.001,wall,mono)
    release=threading.Event();quote_wait=threading.Event();history_wait=threading.Event();stop_publish=threading.Event()
    metadata_threads=[];failures=[];started=time.monotonic()
    def quote(*args):quote_wait.set();assert release.wait(6);raise DataUnavailable('delayed quote')
    def history(*args):history_wait.set();assert release.wait(6);return []
    def metadata(symbol):
        metadata_threads.append(threading.current_thread().name)
        result=deepcopy(templates[symbol]);result.pop('quote',None);result['risk_at']=time.time()
        return result
    monkeypatch.setattr(reader,'position_orderbook',quote);monkeypatch.setattr(reader,'candles',history)
    monkeypatch.setattr(reader,'refresh_position_risk',metadata);monkeypatch.setattr(reader,'funding',lambda *a:[])
    def publish():
        try:
            while not stop_publish.is_set():
                for symbol in symbols:
                    stream.ingest({'type':'snapshot','topic':'tickers.'+symbol,'ts':int(time.time()*1000),
                                   'data':{'symbol':symbol,'bid1Price':'100.4','ask1Price':'100.5','markPrice':'100.45'}},generation=stream._generation)
                    hub.publish_ws_event('tickers',symbol)
                stop_publish.wait(.08)
        except BaseException as exc:failures.append(exc)
    thread=threading.Thread(target=publish);thread.start()
    def stop():
        if time.monotonic()-started>3.3:release.set();return True
        return False
    try:
        run_loop(folder,cfg,store,reader,NeverTrades(),{},stop,lambda *a:100,None)
        saved=Store(folder).read(False)
        assert quote_wait.is_set() and history_wait.is_set() and not failures
        assert all(saved['margin_data'][s]['risk_at']>wall for s in symbols)
        assert margin_status(saved)==''
        assert metadata_threads and all(n.startswith('position-risk') for n in metadata_threads)
        assert len(store.history())==20;store.audit()
    finally:
        release.set();stop_publish.set();thread.join(3);assert not thread.is_alive()


def test_metadata_only_failure_neither_extends_tiers_nor_revokes_independent_quotes(tmp_path):
    cfg,store,_=account(tmp_path,['BTCUSDT'])
    before=store.read(False)
    process_tick(cfg,store,NeverTrades(),MarketTick('BTCUSDT',[],None,[],{},risk_only=True,risk_error='tiers unavailable'))
    after=Store(store.folder).read(False)
    assert after['margin_data']==before['margin_data'] and after['marks']==before['marks']
    assert after['risk_metadata_errors']['BTCUSDT']=='tiers unavailable'
    assert len(store.history())==1;store.audit()


def test_older_metadata_completion_cannot_rollback_fresh_tiers():
    row={'symbol':'BTCUSDT','tiers':[{'new':True}],'risk_at':time.time()-1}
    state={'margin_data':{'BTCUSDT':deepcopy(row)},'positions':{'p':{'symbol':'BTCUSDT'}},'pending':{}}
    attach_snapshot(state,'BTCUSDT',{'margin_data':dict(row,tiers=[{'old':True}],risk_at=row['risk_at']-120)})
    assert state['margin_data']['BTCUSDT']==row


def test_refresh_respects_source_age_and_requests_existing_position_priority(monkeypatch):
    source=PublicData();seen=[]
    def snapshot(symbol):
        seen.append((source._thread.position_mark,source._thread.risk_metadata_only))
        return {'symbol':symbol,'tiers':[1],'risk_at':time.time()-239}
    monkeypatch.setattr(source,'margin_snapshot',snapshot)
    try:
        start=time.monotonic();value=source.refresh_position_risk('BTCUSDT')
        assert seen==[(True,True)] and value['risk_at']<time.time()-238
        assert source._risk_refresh_due['BTCUSDT']<=start+5.1
        assert not source._thread.position_mark and not source._thread.risk_metadata_only
    finally:source.close()


def test_foreign_depth_adapter_requires_explicit_risk_transport(monkeypatch):
    source=PublicData()
    try:
        assert source.risk_maintenance_enabled()
        monkeypatch.setattr(source,'orderbook',lambda *a:None)
        assert not source.risk_maintenance_enabled()
        monkeypatch.setattr(source,'refresh_position_risk',lambda symbol:None)
        assert source.risk_maintenance_enabled()
    finally:source.close()
