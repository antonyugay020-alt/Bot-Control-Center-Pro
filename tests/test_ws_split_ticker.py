"""Reproduce V5 lifetime coupling and verify independent original evidence."""
import time
from types import SimpleNamespace

import pytest

from bcc import data_cache, market_clock, ws_split_ticker
from bcc.data_cache import PublicCache
from bcc.market import PublicData
from bcc.market_hub import HubRegistry, MarketDataHub
from bcc.ws_dual_quotes import DualBybitQuoteStream
from bcc.ws_source_deadline import remaining_bybit_ws_seconds
from bcc.ws_orderbooks import BybitOrderbookWS

HOST = 'api.bybit.com'
TICKER = 'https://api.bybit.com/v5/market/tickers'
BOOK = 'https://api.bybit.com/v5/market/orderbook'
PARAMS = dict(category='linear', symbol='BTCUSDT')
A, B = 'a' * 32, 'b' * 32


@pytest.fixture
def clock(monkeypatch):
    utc = [round(time.time(), 3)]
    wall = utc.copy()
    bounds = lambda stamp: (utc[0]-stamp, utc[0]-stamp+.05)
    monkeypatch.setattr(ws_split_ticker, 'book_age_bounds', bounds)
    monkeypatch.setattr(market_clock, 'book_age_bounds', bounds)
    monkeypatch.setattr('bcc.ws_source_deadline.book_age_bounds', bounds)
    monkeypatch.setattr(data_cache, 'time', SimpleNamespace(time=lambda: wall[0], monotonic=time.monotonic, sleep=time.sleep))
    return utc, wall


def publication(symbol, mark, quote):
    return dict(url=TICKER, params=dict(category='linear',symbol=symbol), host=HOST,
                value=ws_split_ticker.make_split_ticker(symbol, mark, quote), ttl=1.5)


@pytest.fixture
def cache(tmp_path):
    registry = HubRegistry(tmp_path/'isolated.sqlite')
    cache = PublicCache(registry.path)
    assert registry.claim_leader('owner')
    registry.subscribe(TICKER, PARAMS, priority=True)
    cache.bybit_ws_source_deadlines = True
    try:
        yield registry, cache
    finally:
        cache.close()


def publish(registry, cache, records):
    return cache.publish_websockets(records, leader_owner='owner', leader_token=registry.leader_token('owner'))


@pytest.mark.parametrize('mark_first',[False, True])
def test_each_field_survives_other_field_expiry_in_all_cache_reads(clock, cache, mark_first):
    utc, wall = clock
    registry, reader = cache
    now = utc[0]
    mark_at = now-2.4 if mark_first else now
    quote_at = now if mark_first else now-1.8
    record = publication('BTCUSDT', (100.5,mark_at,A), (100.,101.,quote_at,B))
    assert publish(registry,reader,[record])==[True]
    both = reader.read_fresh(TICKER,PARAMS,HOST)
    assert both['_bcc_ws_mark_fence']==A and both['_bcc_ws_quote_fence']==B
    assert both['_bcc_ws_connection_fence'] is None
    utc[0]+= .8; wall[0]+=.8
    one=reader.read_fresh(TICKER,PARAMS,HOST)
    assert one is not None
    assert reader.read_fresh_many([(TICKER,PARAMS)],HOST)==[one]
    assert reader.begin(TICKER,PARAMS,HOST,websocket=True)[2]==one
    row=one['result']['list'][0]
    assert one['_bcc_ws_mark'] is (not mark_first)
    assert one['_bcc_ws_quote'] is mark_first
    if mark_first:
        assert 'markPrice' not in row and one['_bcc_ws_mark_fence'] is None
        assert one['time']==round(quote_at*1000)
    else:
        assert 'bid1Price' not in row and 'ask1Price' not in row
        assert one['_bcc_ws_quote_fence'] is None and one['_bcc_ws_mark_fence']==A
        assert one['_bcc_ws_mark_at']==round(mark_at*1000)
        assert one['time']==one['_bcc_ws_mark_at']
    # Clock rollback must not revive a field or the physical row's old contents.
    utc[0]+=3.; wall[0]-=4.
    assert reader.read_fresh(TICKER,PARAMS,HOST) is None
    assert reader.read_fresh_many([(TICKER,PARAMS)],HOST)==[None]
    assert publish(registry,reader,[record])==[False]


def test_v5_min_deadline_failure_reproduced_and_corrected(clock, cache):
    utc,wall=clock; registry,reader=cache; now=utc[0]
    # Both fields valid initially; the quote deadline arrives first.
    value=publication('BTCUSDT',(100.5,now,A),(100.,101.,now-1.7,A))['value']
    legacy={k:v for k,v in value.items() if k not in ('_bcc_ws_split_ticker','_bcc_ws_mark_fence','_bcc_ws_quote_fence')}
    assert 0.<remaining_bybit_ws_seconds(legacy)<.3
    assert publish(registry,reader,[dict(url=TICKER,params=PARAMS,host=HOST,value=legacy,ttl=1.5)])==[True]
    utc[0]+=.5;wall[0]+=.5
    assert remaining_bybit_ws_seconds(legacy)==0.
    projected=reader.read_fresh(TICKER,PARAMS,HOST)
    assert projected['_bcc_ws_mark'] is True and projected['_bcc_ws_quote'] is False
    assert PublicData._decode_ws_position_mark('BTCUSDT',projected)['at']==round(now*1000)/1000


@pytest.mark.parametrize('invalid',[None,'x'*32,'short',True])
def test_invalid_origin_cannot_be_borrowed_from_other_field(clock, cache, invalid):
    utc,_=clock; registry,reader=cache; now=utc[0]
    rec=publication('BTCUSDT',(100.5,now,invalid),(100.,101.,now,B))
    assert publish(registry,reader,[rec])==[True]
    row=reader.read_fresh(TICKER,PARAMS,HOST)
    assert row['_bcc_ws_mark'] is False and row['_bcc_ws_quote'] is True
    assert 'markPrice' not in row['result']['list'][0]


def test_wrong_symbol_and_unowned_lease_are_rejected(clock, cache):
    utc,_=clock; registry,reader=cache
    rec=publication('BTCUSDT',(100.,utc[0],A),None)
    rec['params']=dict(category='linear',symbol='ETHUSDT')
    registry.subscribe(TICKER,rec['params'])
    with pytest.raises(ValueError):
        publish(registry,reader,[rec])
    rec=publication('BTCUSDT',(100.,utc[0],A),None)
    assert reader.publish_websockets([rec],leader_owner='other',leader_token='wrong')==[False]
    assert reader.read_fresh(TICKER,PARAMS,HOST) is None


class Sink:
    def send(self, raw): pass
    def close(self): pass


def frame(child,symbol,now,**fields):
    return child.ingest(dict(type='snapshot',topic='tickers.'+symbol,ts=round(now*1000),data=fields),generation=child._generation)


def test_real_hub_independent_origins_and_no_reuse_after_reconnect(tmp_path,clock):
    utc,wall=clock; now=utc[0]
    stream=DualBybitQuoteStream(wall=lambda:utc[0],monotonic=lambda:utc[0])
    stream._primary.connect(Sink());stream._recovery.connect(Sink())
    hub=MarketDataHub(tmp_path/'hub.sqlite',source=PublicData(quote_stream=stream))
    try:
        assert hub.acquire_leadership()
        hub.source.shared.bybit_ws_source_deadlines=True
        hub.registry.subscribe(TICKER,PARAMS,priority=True)
        hub.subscribe_active_websockets()
        assert frame(stream._primary,'BTCUSDT',now,markPrice='100.5')
        utc[0]+=1.1;wall[0]+=1.1
        stream.refresh_stale_marks(['BTCUSDT'])
        assert frame(stream._recovery,'BTCUSDT',utc[0],bid1Price='100',ask1Price='101')
        assert hub.publish_ws_event('tickers','BTCUSDT')
        result=hub.source.shared.read_fresh(TICKER,PARAMS,HOST)
        assert result['_bcc_ws_mark_fence']==stream._primary._connection_fence
        assert result['_bcc_ws_quote_fence']==stream._recovery._connection_fence
        assert result['_bcc_ws_connection_fence'] is None
        utc[0]+=1.1;wall[0]+=1.1
        assert hub.publish_ws_event('tickers','BTCUSDT')
        result=hub.source.shared.read_fresh(TICKER,PARAMS,HOST)
        assert result['_bcc_ws_mark_at']==round(now*1000)  # not re-stamped by quote
        # Retire mark generation; a new quote-only publication cannot borrow it.
        stream._primary.disconnect();stream._primary.connect(Sink())
        assert frame(stream._primary,'BTCUSDT',utc[0],bid1Price='102',ask1Price='103')
        assert hub.publish_ws_event('tickers','BTCUSDT')
        result=hub.source.shared.read_fresh(TICKER,PARAMS,HOST)
        assert result['_bcc_ws_mark'] is False and result['_bcc_ws_quote'] is True
    finally:
        hub.close();hub.source.close();stream.stop()


def test_500_symbols_24_coherent_sqlite_rounds_with_independent_deadlines(tmp_path,clock):
    utc,wall=clock
    registry=HubRegistry(tmp_path/'cohort.sqlite');reader=PublicCache(registry.path)
    reader.bybit_ws_source_deadlines=True
    assert registry.claim_leader('owner',lifetime=120)
    symbols=[f'Q{i}USDT' for i in range(500)]
    requests=[(TICKER,dict(category='linear',symbol=s)) for s in symbols]
    books=[(BOOK,dict(category='linear',symbol=s,limit=50)) for s in symbols]
    try:
        for url,params in requests+books:registry.subscribe(url,params,lifetime=120,priority=True)
        for rnd in range(24):
            stamp=round(utc[0]*1000)
            records=[publication(s,(100.5,stamp/1000,A),(100.,101.,stamp/1000-1.6,B)) for s in symbols]
            records += [dict(url=BOOK,params=params,host=HOST,ttl=1.5,value=dict(retCode=0,_bcc_ws_book=True,time=stamp,result=dict(s=params['symbol'],ts=stamp,b=[['100','1']],a=[['101','1']],u=rnd+1,seq=rnd+1))) for _,params in books]
            for start in range(0,len(records),64):assert all(publish(registry,reader,records[start:start+64]))
            # The independent quote deadline has elapsed; mark and book still valid.
            utc[0]+=.7;wall[0]+=.7
            results=reader.read_fresh_many(requests+books,HOST)
            assert len(results)==1000
            for s,value in zip(symbols,results[:500]):
                assert value['_bcc_ws_mark'] is True and value['_bcc_ws_quote'] is False
                assert value['_bcc_ws_mark_at']==stamp
                assert value['result']['list'][0]['symbol']==s
            for s,value in zip(symbols,results[500:]):
                assert value['_bcc_ws_book'] is True and value['result']['ts']==stamp
                assert value['result']['s']==s
            utc[0]+=.3;wall[0]+=.3
    finally:
        reader.close()
