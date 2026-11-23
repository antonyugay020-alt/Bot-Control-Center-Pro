"""Reproduce quote-only replacement of an otherwise valid independent WS mark."""
import time
from bcc.data_cache import PublicCache
from bcc.market import PublicData
from bcc.market_hub import MarketDataHub
from bcc.ws_quotes import BybitQuoteStream

URL = 'https://api.bybit.com/v5/market/tickers'
PARAMS = dict(category='linear', symbol='DOGEUSDT')
HOST = 'api.bybit.com'


def test_fresh_real_mark_not_erased_by_new_quote_only_delta(tmp_path):
    # The underlying mark event is < 3 sec old by the consumer's real clock,
    # though the producer's shorter mark window has just expired.
    simulated = [time.time() - 0.15]
    stream = BybitQuoteStream(max_age=0.1, wall=lambda: simulated[0], monotonic=lambda: simulated[0])
    stream.subscribe('DOGEUSDT')
    generation = stream.connect()
    hub = MarketDataHub(tmp_path / 'isolated.sqlite', source=PublicData(quote_stream=stream))
    cache = PublicCache(tmp_path / 'isolated.sqlite')
    try:
        assert hub.acquire_leadership()
        hub.registry.subscribe(URL, PARAMS)
        def tick(mark=False):
            row = {'symbol':'DOGEUSDT', 'bid1Price':'100', 'ask1Price':'101'}
            if mark: row['markPrice']='100.5'
            return stream.ingest({'type':'snapshot' if mark else 'delta',
                                  'topic':'tickers.DOGEUSDT',
                                  'ts':int(simulated[0]*1000), 'data':row}, generation=generation)
        assert tick(mark=True)
        assert hub.publish_ws_event('tickers','DOGEUSDT')
        original = cache.read_fresh(URL, PARAMS, HOST)
        assert original['_bcc_ws_mark'] is True
        original_at = original['_bcc_ws_mark_at']
        PublicData._decode_ws_position_mark('DOGEUSDT', original)

        simulated[0] += 0.12
        assert tick(mark=False)
        assert hub.publish_ws_event('tickers','DOGEUSDT')
        after = cache.read_fresh(URL, PARAMS, HOST)
        assert after['_bcc_ws_quote'] is True
        assert after['_bcc_ws_mark'] is True, 'quote-only publication erased a still-authentic mark'
        assert after['_bcc_ws_mark_at'] == original_at
        PublicData._decode_ws_position_mark('DOGEUSDT',after)
    finally:
        cache.close(); hub.close(); hub.source.close()


def test_reconnect_cannot_resurrect_cached_mark_from_prior_socket(tmp_path):
    simulated = [time.time() - .15]
    stream = BybitQuoteStream(max_age=.1, wall=lambda: simulated[0], monotonic=lambda: simulated[0])
    stream.subscribe('DOGEUSDT')
    g1 = stream.connect()
    hub = MarketDataHub(tmp_path / 'isolated.sqlite', source=PublicData(quote_stream=stream))
    cache = PublicCache(tmp_path / 'isolated.sqlite')
    try:
        assert hub.acquire_leadership()
        hub.registry.subscribe(URL, PARAMS)
        assert stream.ingest({'type':'snapshot', 'topic':'tickers.DOGEUSDT',
            'ts':int(simulated[0]*1000), 'data':{'symbol':'DOGEUSDT',
            'bid1Price':'100','ask1Price':'101','markPrice':'100.5'}}, generation=g1)
        assert hub.publish_ws_event('tickers','DOGEUSDT')
        prior = cache.read_fresh(URL,PARAMS,HOST)
        old_fence = prior['_bcc_ws_connection_fence']
        assert prior['_bcc_ws_mark'] is True
        stream.disconnect()
        g2 = stream.connect()
        assert g2 != g1
        simulated[0] += .02
        assert stream.ingest({'type':'snapshot', 'topic':'tickers.DOGEUSDT',
            'ts':int(simulated[0]*1000), 'data':{'symbol':'DOGEUSDT',
            'bid1Price':'100.1','ask1Price':'101.1'}}, generation=g2)
        assert hub.publish_ws_event('tickers','DOGEUSDT')
        new = cache.read_fresh(URL,PARAMS,HOST)
        assert new['_bcc_ws_connection_fence'] != old_fence
        assert new['_bcc_ws_quote'] is True
        assert new['_bcc_ws_mark'] is False
        try:
            PublicData._decode_ws_position_mark('DOGEUSDT',new)
        except Exception as exc:
            assert 'not confirmed' in str(exc)
        else:
            raise AssertionError('Old-generation mark leaked into a new socket')
    finally:
        cache.close();hub.close();hub.source.close()


def test_expired_exchange_mark_not_renewed_by_later_quote(tmp_path):
    import json
    import hashlib
    simulated=[time.time()-.15]
    stream=BybitQuoteStream(max_age=.1,wall=lambda:simulated[0],monotonic=lambda:simulated[0])
    stream.subscribe('DOGEUSDT');gen=stream.connect()
    hub=MarketDataHub(tmp_path/'isolated.sqlite',source=PublicData(quote_stream=stream))
    cache=PublicCache(tmp_path/'isolated.sqlite')
    try:
        assert hub.acquire_leadership()
        hub.registry.subscribe(URL,PARAMS)
        def feed(mark):
            data={'symbol':'DOGEUSDT','bid1Price':'100','ask1Price':'101'}
            if mark:data['markPrice']='100.5'
            return stream.ingest({'type':'snapshot' if mark else 'delta',
                'topic':'tickers.DOGEUSDT','ts':int(simulated[0]*1000),
                'data':data},generation=gen)
        assert feed(True)
        assert hub.publish_ws_event('tickers','DOGEUSDT')
        # Force an authentically old 4s timestamp into an otherwise unexpired
        # cached mark: cache TTL alone must never make it a trusted mark.
        key=hashlib.sha256(json.dumps([URL,PARAMS],sort_keys=True).encode()).hexdigest()
        with cache.db() as db:
            old=json.loads(db.execute('SELECT value FROM cache WHERE k=?',(key,)).fetchone()[0])
            old['_bcc_ws_mark_at']=int((time.time()-4)*1000)
            db.execute('UPDATE cache SET value=? WHERE k=?',(json.dumps(old),key))
        simulated[0]+=.12
        assert feed(False)
        assert hub.publish_ws_event('tickers','DOGEUSDT')
        row=cache.read_fresh(URL,PARAMS,HOST)
        assert row['_bcc_ws_quote'] is True
        assert row['_bcc_ws_mark'] is False
    finally:
        cache.close();hub.close();hub.source.close()


def test_first_quote_only_update_does_not_claim_mark(tmp_path):
    now=time.time()
    stream=BybitQuoteStream(max_age=2)
    stream.subscribe('DOGEUSDT');gen=stream.connect()
    hub=MarketDataHub(tmp_path/'isolated.sqlite',source=PublicData(quote_stream=stream))
    cache=PublicCache(tmp_path/'isolated.sqlite')
    try:
        assert hub.acquire_leadership()
        hub.registry.subscribe(URL,PARAMS)
        assert stream.ingest({'type':'snapshot','topic':'tickers.DOGEUSDT',
            'ts':int(now*1000),'data':{'symbol':'DOGEUSDT',
            'bid1Price':'100','ask1Price':'101'}},generation=gen)
        assert hub.publish_ws_event('tickers','DOGEUSDT')
        row=cache.read_fresh(URL,PARAMS,HOST)
        assert row['_bcc_ws_quote'] is True
        assert row['_bcc_ws_mark'] is False
    finally:
        cache.close();hub.close();hub.source.close()
