"""Real one-character Bybit instruments must reach every public WS route."""
import time

import pytest

from bcc import market_clock
from bcc.market import PublicData
from bcc.market_hub import MarketDataHub
from bcc.ws_quotes import BybitQuoteStream
from bcc.ws_orderbooks import BybitOrderbookWS
from bcc.ws_klines import BybitKlineWS
from bcc.ws_binance import BinanceTradeWS


@pytest.mark.parametrize('symbol', ['AUSDT', '4USDT'])
def test_short_asset_reaches_all_bybit_hub_cache_consumers(tmp_path, monkeypatch, symbol):
    now = time.time()
    stamp = int(now * 1000)
    reference = market_clock.MarketClock()
    monkeypatch.setattr(market_clock, 'clock', reference)
    monkeypatch.setattr('bcc.market.clock', reference)
    quotes = BybitQuoteStream(wall=lambda: now)
    books = BybitOrderbookWS(wall=lambda: now)
    candles = BybitKlineWS(wall=lambda: now)
    producer = PublicData(quote_stream=quotes, orderbook_stream=books, kline_stream=candles)
    path = tmp_path / 'market.sqlite'
    hub = MarketDataHub(path, source=producer)
    reader = None
    base = 'https://api.bybit.com/v5/market/'
    ticker_params = {'category':'linear','symbol':symbol}
    book_params = dict(ticker_params, limit=50)
    candle_params = dict(ticker_params, interval='1', limit=10)
    try:
        for route, params in [('tickers',ticker_params),('orderbook',book_params),('kline',candle_params)]:
            hub.registry.subscribe(base+route,params)
        assert hub.acquire_leadership()
        hub.subscribe_active_websockets()
        # Discovery must not silently omit valid short symbols.
        assert symbol in quotes._subscriptions
        assert symbol in books._symbols
        assert symbol in candles._symbols
        assert quotes.ingest({'type':'snapshot','topic':'tickers.'+symbol,'ts':stamp,
                              'data':{'symbol':symbol,'bid1Price':'.4','ask1Price':'.41','markPrice':'.405'}},
                             generation=quotes.connect())
        assert books.ingest({'type':'snapshot','topic':'orderbook.50.'+symbol,'ts':stamp,
                             'data':{'s':symbol,'u':1,'seq':1,'b':[['.4','100']], 'a':[['.41','100']]}},
                            generation=books.connect())
        start = stamp//60000*60000
        assert candles.ingest({'type':'snapshot','topic':'kline.1.'+symbol,'ts':stamp,
                               'data':[{'start':start,'end':start+59999,'interval':'1',
                                        'open':'.4','high':'.42','low':'.39','close':'.405',
                                        'volume':'100','confirm':False}]}, generation=candles.connect())
        for kind in ('tickers','depth50','kline1'):
            assert hub.publish_ws_event(kind,symbol)
        for key in ('QUOTES','BOOKS','KLINES'):
            monkeypatch.setenv('BCC_BYBIT_WS_'+key,'1')
        monkeypatch.setenv('BCC_PUBLIC_CACHE',str(path))
        monkeypatch.setenv('BCC_MARKET_HUB_MODE','client')
        reader=PublicData()
        def forbidden(*args,**kwargs):
            raise AssertionError('REST cannot replace WS subscription data')
        monkeypatch.setattr(reader.session,'get',forbidden)
        assert reader.get(base+'tickers',ticker_params)['_bcc_ws_quote'] is True
        assert reader._ws_position_mark(symbol)['bid']==.405
        assert reader.get(base+'orderbook',book_params)['result']['s']==symbol
        assert reader.get(base+'kline',candle_params)['_bcc_ws_kline'] is True
    finally:
        if reader is not None: reader.close()
        hub.close()
        producer.close()


@pytest.mark.parametrize('factory', [BybitQuoteStream,BybitOrderbookWS,BybitKlineWS,BinanceTradeWS])
@pytest.mark.parametrize('symbol', ['USDT','aUSDT','AUSDT\n','A/USDT','A.USDT','A'*25+'USDT'])
def test_shared_symbol_contract_still_rejects_malformed_topics(factory,symbol):
    with pytest.raises(ValueError):
        factory().subscribe(symbol)
