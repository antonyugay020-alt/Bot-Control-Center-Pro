"""An account commits a fresh WS portfolio together, rather than a slow N-mark sweep."""
import time
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from bcc import market_clock
from bcc.config import defaults, range_section
from bcc.market import PublicData, SourceAccessDenied
from bcc.market_hub import MarketDataHub
from bcc.market_clock import observed_quote, quote_is_recent
from bcc.margin import margin_status, attach_snapshot
from bcc.runtime import process_tick
from bcc.scheduler import MarketTick
from bcc.storage import Repository, Store
from bcc.trading import enter, plan
from bcc.ws_quotes import BybitQuoteStream
from bcc.ws_orderbooks import BybitOrderbookWS
from test_core import attach
from test_margin_finance import use_mark, raw_tiers
from test_bybit_ws_position_fast_lane import _instrument


@pytest.fixture
def portfolio(tmp_path,monkeypatch):
    clock=market_clock.MarketClock()
    monkeypatch.setattr(market_clock,'clock',clock)
    monkeypatch.setattr('bcc.market.clock',clock)
    monkeypatch.setattr(clock,'ensure',lambda:None)
    symbols=[f'COIN{i}USDT' for i in range(20)]
    q=BybitQuoteStream()
    q.subscribe_many(symbols)
    generation=q.connect()
    books=BybitOrderbookWS()
    books.subscribe(symbols[0])
    book_generation=books.connect()
    path=tmp_path/'hub/market.sqlite'
    producer=PublicData(quote_stream=q,orderbook_stream=books)
    hub=MarketDataHub(path,source=producer)
    ticker='https://api.bybit.com/v5/market/tickers'
    for symbol in symbols:
        hub.registry.subscribe(ticker,{'category':'linear','symbol':symbol})
    hub.registry.subscribe('https://api.bybit.com/v5/market/orderbook',
                           {'category':'linear','symbol':symbols[0],'limit':50})
    assert hub.acquire_leadership()
    stamp=int(time.time()*1000)
    for symbol in symbols:
        assert q.ingest({'topic':'tickers.'+symbol,'type':'snapshot','ts':stamp,
                         'data':{'symbol':symbol,'bid1Price':'100.4','ask1Price':'100.5','markPrice':'100.45'}},
                        generation=generation)
        assert hub.publish_ws_event('tickers',symbol)
    assert books.ingest({'topic':'orderbook.50.'+symbols[0],'type':'snapshot','ts':stamp,
                         'data':{'s':symbols[0],'u':1,'seq':1,'b':[['100.4','1000']], 'a':[['100.5','1000']]}},
                        generation=book_generation)
    assert hub.publish_ws_event('depth50',symbols[0])
    for key,value in {'BCC_PUBLIC_CACHE':str(path),'BCC_MARKET_HUB_MODE':'client',
                      'BCC_BYBIT_WS_QUOTES':'1','BCC_BYBIT_WS_BOOKS':'1'}.items():
        monkeypatch.setenv(key,value)
    reader=PublicData()
    reader.set_position_symbols(symbols)
    get=reader.get
    def metadata(url,params=None,**kwargs):
        if url.endswith('/instruments-info'):
            return _instrument(params['symbol'])
        return get(url,params,**kwargs)
    monkeypatch.setattr(reader,'get',metadata)
    def forbidden(*args,**kwargs):raise AssertionError('No REST fallback for portfolio marks')
    monkeypatch.setattr(reader.session,'get',forbidden)
    try:
        yield symbols,reader,hub,q
    finally:
        reader.close();hub.close();producer.close()


@pytest.mark.parametrize('lagging_book',[False,True])
def test_entire_exposure_updates_in_one_real_worker_sqlite_transaction(portfolio,tmp_path,lagging_book):
    symbols,reader,_,_=portfolio
    cfg=defaults('VELES');cfg['name']='Portfolio cohort';cfg['symbols']=symbols;cfg['sections']=[range_section(30,40)]
    cfg['settings']['orders']['count']=1
    cfg['settings']['profit']['pct']=50
    repo=Repository(tmp_path/'account');folder=repo.create(cfg);store=Store(folder,10000)
    with store.transaction() as (state,events):
        for symbol in symbols:
            attach(state,symbol)
            use_mark(state,symbol=symbol)
            ok,why=enter(state,events,symbol+':'+cfg['sections'][0]['id'],symbol,'LONG',
                         cfg['settings'],100,25,plan(cfg['settings']['orders'],'LONG',100),cfg['sections'][0]['id'])
            assert ok,why
            state['marks'][symbol]=observed_quote({'bid':100,'ask':100.01,'at':time.time()})
        for symbol in symbols:
            state['margin_data'][symbol]['quote']=observed_quote({'bid':100,'ask':100,'at':time.time()-6})
        state.pop('_books',None)
    assert 'mark-price' in margin_status(store.read())
    book=reader.position_orderbook(symbols[0])
    assert len(book['margin_marks'])==len(book['portfolio_quotes'])==20
    if lagging_book:
        # The real execution book is still admitted, while the independently
        # timed quote already available to the worker is two seconds newer.
        book['at']-=2
    process_tick(cfg,store,None,MarketTick(symbols[0],[],book,[],{}))
    restored=Store(folder);saved=restored.read()
    assert margin_status(saved)==''
    assert len(saved['positions'])==20
    assert len(restored.history())==20  # No unrequested fills from the price batch.
    assert all(quote_is_recent(saved['marks'][s],maximum=3) for s in symbols)
    assert saved['marks'][symbols[0]]['at']>=book['portfolio_quotes'][symbols[0]]['at']
    restored.audit()


def test_batch_is_coalesced_without_changing_original_observation_receipts(portfolio,monkeypatch):
    _,reader,_,_=portfolio
    calls=[];original=reader.shared.read_fresh_many
    def spy(*args,**kwargs):calls.append(1);return original(*args,**kwargs)
    monkeypatch.setattr(reader.shared,'read_fresh_many',spy)
    with ThreadPoolExecutor(max_workers=4) as pool:
        frames=list(pool.map(lambda _:reader._ws_portfolio_observations(),range(4)))
    assert len(calls)==1
    assert all(frame==frames[0] for frame in frames)


def test_live_portfolio_uses_reserved_reader_during_scanner_saturation(portfolio):
    symbols,reader,_,_=portfolio
    wall,mono=time.time(),time.monotonic()
    market_clock.clock._reference=(wall,wall+.001,wall,mono)
    entered=[threading.Event() for _ in range(3)]
    release=threading.Event();threads=[];errors=[]
    def scanner(index):
        try:
            with reader.shared.read_db():
                entered[index].set()
                assert release.wait(5)
        except BaseException as exc:errors.append(exc)
    try:
        for index in range(3):
            thread=threading.Thread(target=scanner,args=(index,));thread.start();threads.append(thread)
            assert entered[index].wait(2)
        marks,quotes=reader.cached_position_observations()
        assert set(marks)==set(symbols)
        assert symbols[0] in quotes
    finally:
        release.set()
        for thread in threads:thread.join(5)
    assert not errors and all(not thread.is_alive() for thread in threads)


def test_cached_batch_still_obeys_local_and_shared_provider_denial(portfolio):
    _,reader,_,_=portfolio
    assert reader._ws_portfolio_observations()[0]
    reader._access_denials['api.bybit.com']=(time.monotonic()+60,403)
    with pytest.raises(SourceAccessDenied) as error:
        reader._ws_portfolio_observations()
    assert error.value.http_status==403
    reader._access_denials.clear()
    with reader.shared.db() as db:
        db.execute('BEGIN IMMEDIATE')
        reader.shared._store_denial(db,'api.bybit.com',60,451)
        db.commit()
    with pytest.raises(SourceAccessDenied) as error:
        reader._ws_portfolio_observations()
    assert error.value.http_status==451


def test_slow_risk_tiers_do_not_erase_or_regress_newer_mark():
    state={'positions':{},'pending':{},'margin_data':{}}
    old=observed_quote({'bid':100,'ask':100,'at':time.time()-.2})
    state['margin_data']['BTCUSDT']={'symbol':'BTCUSDT','tiers':[1],'quote':old}
    attach_snapshot(state,'BTCUSDT',{'margin_data':{'symbol':'BTCUSDT','tiers':[2]},'margin_marks':{}})
    assert state['margin_data']['BTCUSDT']['quote']==old
    older=observed_quote({'bid':99,'ask':99,'at':time.time()-1})
    attach_snapshot(state,'BTCUSDT',{'margin_marks':{'BTCUSDT':older}})
    assert state['margin_data']['BTCUSDT']['quote']==old


@pytest.mark.parametrize('damage', ['rest','stale_mark','mark_only','stale_quote','wrong_symbol','absent'])
def test_portfolio_keeps_independent_provenance_and_age_fences(portfolio,damage):
    import hashlib,json
    symbols,reader,_,_=portfolio
    symbol=symbols[-1]
    url='https://api.bybit.com/v5/market/tickers'
    params={'category':'linear','symbol':symbol}
    key=hashlib.sha256(json.dumps([url,params],sort_keys=True).encode()).hexdigest()
    with reader.shared.db() as db:
        value=json.loads(db.execute('SELECT value FROM cache WHERE k=?',(key,)).fetchone()[0])
        if damage=='rest':
            value['_bcc_ws_mark']=value['_bcc_ws_quote']=False
        elif damage=='stale_mark':value['_bcc_ws_mark_at']=int((time.time()-10)*1000)
        elif damage=='mark_only':value['_bcc_ws_quote']=False
        elif damage=='stale_quote':value['time']=int((time.time()-10)*1000)
        elif damage=='wrong_symbol':value['result']['list'][0]['symbol']='BTCUSDT'
        if damage=='absent':db.execute('DELETE FROM cache WHERE k=?',(key,))
        else:db.execute('UPDATE cache SET value=? WHERE k=?',(json.dumps(value),key))
    marks,quotes=reader._ws_portfolio_observations()
    if damage in ('rest','stale_mark','wrong_symbol','absent'):assert symbol not in marks
    else:assert symbol in marks
    if damage in ('rest','mark_only','stale_quote','wrong_symbol','absent'):assert symbol not in quotes
    else:assert symbol in quotes
    assert len(marks)>=19 and len(quotes)>=19


def test_scanner_risk_tiers_cannot_grant_rest_collateral_in_ws_mode(portfolio,monkeypatch):
    symbols,reader,_,_=portfolio
    get=reader.get
    def risk(url,params=None,**kwargs):
        if url.endswith('/risk-limit'):
            return {'retCode':0,'time':int(time.time()*1000),
                    'result':{'list':raw_tiers(params['symbol'])}}
        if url.endswith('/tickers') and not params.get('symbol'):
            raise AssertionError('REST portfolio ticker must not renew WS collateral')
        return get(url,params,**kwargs)
    monkeypatch.setattr(reader,'get',risk)
    snapshot=reader.margin_snapshot(symbols[0])
    assert len(snapshot['account_marks'])==20
    assert snapshot['quote']['bid']==100.45
    assert snapshot['tiers']


def test_large_cache_read_is_one_read_transaction_without_budget_or_leases(tmp_path):
    import hashlib,json
    from bcc.data_cache import PublicCache
    cache=PublicCache(tmp_path/'cache.sqlite')
    requests=[('https://api.bybit.com/v5/market/tickers',{'category':'linear','symbol':f'C{i}USDT'}) for i in range(1001)]
    try:
        cache._ensure_schema()
        with cache.db() as db:
            db.executemany('INSERT INTO cache(k,value,until,host) VALUES(?,?,?,?)',[
                (hashlib.sha256(json.dumps([url,p],sort_keys=True).encode()).hexdigest(),
                 json.dumps({'symbol':p['symbol']}),time.time()+60,'api.bybit.com') for url,p in requests])
        with cache.read_db() as db:
            trace=[]
            db.set_trace_callback(trace.append)
        values=cache.read_fresh_many(requests,'api.bybit.com')
        assert [v['symbol'] for v in values]==[p['symbol'] for _,p in requests]
        assert trace.count('BEGIN')==trace.count('ROLLBACK')==1
        with cache.db() as db:
            assert db.execute('SELECT count(*) FROM lease').fetchone()[0]==0
            assert db.execute('SELECT count(*) FROM request_budget').fetchone()[0]==0
    finally:
        cache.close()


def test_risk_metadata_refresh_does_not_depend_on_either_price_transport(portfolio,monkeypatch):
    symbols,reader,_,_=portfolio
    def risk(url,params=None,**kwargs):
        assert url.endswith('/risk-limit'), 'Risk tiers must not poll WS or REST prices'
        return {'retCode':0,'time':int(time.time()*1000),
                'result':{'list':raw_tiers(params['symbol'])}}
    monkeypatch.setattr(reader,'get',risk)
    snapshot=reader.refresh_position_risk(symbols[0])
    assert snapshot['tiers']
    assert 'quote' not in snapshot and 'account_marks' not in snapshot
    assert not getattr(reader._thread,'risk_metadata_only',False)


def test_new_scanner_ticker_has_bounded_subscription_grace_off_the_fast_lane(portfolio,monkeypatch):
    symbols,reader,_,_=portfolio
    reader.set_position_symbols([])
    get=reader.get
    def risk(url,params=None,**kwargs):
        if url.endswith('/risk-limit'):
            return {'retCode':0,'time':int(time.time()*1000),
                    'result':{'list':raw_tiers(params['symbol'])}}
        return get(url,params,**kwargs)
    monkeypatch.setattr(reader,'get',risk)
    read=reader.shared.read_fresh
    started=time.monotonic()
    def delayed(url,params,host):
        if url.endswith('/tickers'):
            if time.monotonic()-started < .95:
                return None
            return {'retCode':0,'_bcc_ws_mark':True,'_bcc_ws_mark_at':int(time.time()*1000),
                    'result':{'list':[{'symbol':params['symbol'],'markPrice':'100.45'}]}}
        return read(url,params,host)
    monkeypatch.setattr(reader.shared,'read_fresh',delayed)
    result=reader.margin_snapshot(symbols[0])
    assert result['quote']['bid']==100.45
    assert not getattr(reader._thread,'mark_subscription_warmup',False)


def test_cohort_fast_path_still_renews_the_current_position_ticker_lease(portfolio):
    symbols,reader,hub,_=portfolio
    assert reader._ws_portfolio_observations()[0]
    key=hub.registry.identity('https://api.bybit.com/v5/market/tickers',
                              {'category':'linear','symbol':symbols[0]})[0]
    with hub.registry._connect() as db:
        assert db.execute('SELECT priority_until FROM hub_subscriptions WHERE k=?',(key,)).fetchone()[0]==0
    reader.position_orderbook(symbols[0])
    with hub.registry._connect() as db:
        active,priority=db.execute('SELECT active_until,priority_until FROM hub_subscriptions WHERE k=?',(key,)).fetchone()
    assert active > time.time() and priority > time.time()


def test_execution_depth_cache_does_not_retain_an_entire_portfolio_per_symbol():
    import gc,weakref
    from bcc.runtime import _execution_depth
    class Cohort(dict):pass
    observations=Cohort({f'C{i}USDT':{'bid':100,'ask':101} for i in range(1000)})
    reference=weakref.ref(observations)
    book={'at':time.time(),'bids':[[100,10]],'asks':[[101,10]],
          'qty_step':.001,'min_qty':.001,'min_notional':5,'tick_size':.01,
          'portfolio_quotes':observations,'margin_marks':observations}
    cached=_execution_depth(book)
    del book,observations
    gc.collect()
    assert reference() is None
    assert cached['bids']==[[100,10]] and cached['asks']==[[101,10]]
    assert cached['qty_step']==.001


def test_mark_only_ticker_cannot_hide_independently_fresh_held_depth(portfolio):
    symbols,reader,hub,stream=portfolio
    symbol=symbols[0]
    stream._quotes.pop(symbol,None)  # Independent ticker sides are absent.
    assert stream.ingest({'type':'snapshot','topic':'tickers.'+symbol,'ts':int(time.time()*1000),
                          'data':{'symbol':symbol,'markPrice':'100.45'}},generation=stream._generation)
    assert hub.publish_ws_event('tickers',symbol)
    marks,quotes=reader._ws_portfolio_observations()
    assert symbol in marks and symbol in quotes
    assert quotes[symbol]['bid']==100.4 and quotes[symbol]['ask']==100.5


@pytest.mark.parametrize('fault',['rest','symbol','crossed','old','quantity','nonfinite'])
def test_invalid_depth_cannot_value_a_mark_only_ticker(portfolio,monkeypatch,fault):
    symbols,reader,hub,stream=portfolio;symbol=symbols[0]
    stream._quotes.pop(symbol,None)
    assert stream.ingest({'type':'snapshot','topic':'tickers.'+symbol,'ts':int(time.time()*1000),
                          'data':{'symbol':symbol,'markPrice':'100.45'}},generation=stream._generation)
    assert hub.publish_ws_event('tickers',symbol)
    original=reader.shared.read_fresh_many
    def corrupted(*args,**kwargs):
        from copy import deepcopy
        rows=deepcopy(original(*args,**kwargs));book=rows[len(symbols)]
        if fault=='rest':book.pop('_bcc_ws_book')
        if fault=='symbol':book['result']['s']='OTHERUSDT'
        if fault=='crossed':book['result']['b'][0][0]='200'
        if fault=='old':book['result']['ts']=int((time.time()-4)*1000)
        if fault=='quantity':book['result']['a'][0][1]='0'
        if fault=='nonfinite':book['result']['a'][0][0]='nan'
        return rows
    monkeypatch.setattr(reader.shared,'read_fresh_many',corrupted)
    marks,quotes=reader._ws_portfolio_observations()
    assert symbol in marks and symbol not in quotes
