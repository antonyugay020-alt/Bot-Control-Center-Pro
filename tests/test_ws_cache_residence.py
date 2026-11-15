"""Quiet WS snapshot cadence, cache residence and original-age fences agree."""
import time
from types import SimpleNamespace

import pytest

from bcc import market_clock
from bcc.market import PublicData
from bcc.market_hub import MarketDataHub
from bcc.ws_quotes import BybitQuoteStream
from bcc.ws_orderbooks import BybitOrderbookWS


@pytest.mark.parametrize('original_age',[0.,1.8])
def test_quiet_resnapshot_gap_keeps_real_quotes_but_never_extends_exchange_age(tmp_path,monkeypatch,original_age):
    from bcc import data_cache
    timer=[time.time()]
    controlled=SimpleNamespace(time=lambda:timer[0],monotonic=time.monotonic,sleep=time.sleep)
    monkeypatch.setattr(data_cache,'time',controlled)
    monkeypatch.setattr(market_clock,'time',controlled)
    reference=market_clock.MarketClock();monkeypatch.setattr(market_clock,'clock',reference)
    monkeypatch.setattr('bcc.market.clock',reference)
    ticker=BybitQuoteStream(wall=lambda:timer[0]);ticker.subscribe('AUSDT');ticker.connect()
    book=BybitOrderbookWS(wall=lambda:timer[0]);book.subscribe('AUSDT');book.connect()
    source=PublicData(quote_stream=ticker,orderbook_stream=book)
    path=tmp_path/'cache.sqlite';hub=MarketDataHub(path,source=source);reader=None
    try:
        assert hub.acquire_leadership()
        hub.registry.subscribe('https://api.bybit.com/v5/market/tickers',{'category':'linear','symbol':'AUSDT'})
        hub.registry.subscribe('https://api.bybit.com/v5/market/orderbook',{'category':'linear','symbol':'AUSDT','limit':50})
        stamp=int((timer[0]-original_age)*1000)
        assert ticker.ingest({'type':'snapshot','topic':'tickers.AUSDT','ts':stamp,
                              'data':{'symbol':'AUSDT','bid1Price':'100','ask1Price':'101','markPrice':'100.5'}},generation=ticker._generation)
        assert book.ingest({'type':'snapshot','topic':'orderbook.50.AUSDT','ts':stamp,
                            'data':{'s':'AUSDT','u':1,'seq':1,'b':[['100','10']],'a':[['101','10']]}},generation=book._generation)
        assert hub.publish_ws_event('tickers','AUSDT');assert hub.publish_ws_event('depth50','AUSDT')
        monkeypatch.setenv('BCC_PUBLIC_CACHE',str(path));monkeypatch.setenv('BCC_MARKET_HUB_MODE','client')
        reader=PublicData();reader.set_position_symbols(['AUSDT'])
        timer[0]+=1.1  # No event/retimestamp: a real quiet resnapshot interval.
        marks,quotes=reader._ws_portfolio_observations()
        assert set(marks)==set(quotes)=={'AUSDT'}
        assert marks['AUSDT']['at']==quotes['AUSDT']['at']==stamp/1000
        if original_age:
            # Cache residence has not expired, but exchange age HAS expired.
            timer[0]+=.2;reader._portfolio_ws_cache=None
            raw=reader.shared.read_fresh('https://api.bybit.com/v5/market/tickers',{'category':'linear','symbol':'AUSDT'},'api.bybit.com')
            assert raw is not None
            assert reader._ws_portfolio_observations()==({}, {})
    finally:
        if reader is not None:reader.close()
        hub.close();source.close()
