"""Production candidate transport and real Hub cache, no network or orders."""
import time
import pytest

from bcc.market import PublicData, DataUnavailable
from bcc.market_hub import MarketDataHub
from bcc.ws_dual_quotes import DualBybitQuoteStream
from bcc import market_clock, ws_split_ticker

TICKER = 'https://api.bybit.com/v5/market/tickers'
PARAMS = {'category':'linear','symbol':'BTCUSDT'}


class Sink:
    def send(self, raw): pass
    def close(self): pass


def test_candidate_omitted_mark_reaches_real_sqlite_with_two_clocks(tmp_path, monkeypatch):
    now=[round(time.time(),3)]
    bounds=lambda stamp:(now[0]-stamp,now[0]-stamp+.05)
    monkeypatch.setattr(market_clock,'book_age_bounds',bounds)
    monkeypatch.setattr(market_clock,'latest_now',lambda:now[0]+.05)
    monkeypatch.setattr(ws_split_ticker,'book_age_bounds',bounds)
    notifications=[]
    stream=DualBybitQuoteStream(wall=lambda:now[0],monotonic=lambda:now[0],
        maintained=True,on_update=lambda *args:notifications.append(args))
    hub=MarketDataHub(tmp_path/'isolated.sqlite',source=PublicData(quote_stream=stream))
    try:
        hub.source.shared.bybit_ws_source_deadlines=True
        hub.registry.subscribe(TICKER,PARAMS,priority=True)
        assert hub.acquire_leadership()
        stream.sync_symbols(['BTCUSDT'])
        stream._primary.connect(Sink())
        stream._recovery.connect(Sink())
        def frame(cs,kind='delta',**data):
            return stream._primary.ingest(dict(type=kind,topic='tickers.BTCUSDT',
                ts=round(now[0]*1000),cs=cs,data=dict(symbol='BTCUSDT',**data)),
                generation=stream._primary._generation)
        assert frame(10,'snapshot',markPrice='100')
        assert hub.publish_ws_event('tickers','BTCUSDT')
        original=round(now[0]*1000)
        now[0]+=4.
        assert stream.independent_observations_for_hub('BTCUSDT')[0] is None
        notifications.clear()
        assert frame(10,indexPrice='101')
        assert notifications==[('tickers','BTCUSDT')]
        assert hub.publish_ws_event('tickers','BTCUSDT')
        row=hub.source.shared.read_fresh(TICKER,PARAMS,'api.bybit.com')
        assert row['_bcc_ws_mark_explicit_at']==original
        assert row['_bcc_ws_mark_at']==round(now[0]*1000)
        assert row['_bcc_ws_mark_basis']=='snapshot_delta'
        decoded=PublicData._decode_ws_position_mark('BTCUSDT',row)
        assert decoded['bid']==100. and decoded['mark_explicit_at']==original/1000
        assert stream.refresh_stale_marks(['BTCUSDT'])==0
        # A conflicting frame must revoke the price, not inherit it from SQLite.
        now[0]+=.1
        assert frame(9,markPrice='999')
        assert hub.publish_ws_event('tickers','BTCUSDT')
        assert hub.source.shared.read_fresh(TICKER,PARAMS,'api.bybit.com') is None
        now[0]+=.1
        assert frame(11,'snapshot',markPrice='102')
        assert hub.publish_ws_event('tickers','BTCUSDT')
        assert hub.source.shared.read_fresh(TICKER,PARAMS,'api.bybit.com')['_bcc_ws_mark_at']==round(now[0]*1000)
        stream._primary.disconnect()
        assert hub.publish_ws_event('tickers','BTCUSDT')
        assert hub.source.shared.read_fresh(TICKER,PARAMS,'api.bybit.com') is None
    finally:
        hub.close();hub.source.close();stream.stop()


def test_malformed_maintained_provenance_cannot_be_projected(monkeypatch):
    now=round(time.time(),3)
    monkeypatch.setattr(ws_split_ticker,'book_age_bounds',lambda t:(now-t,now-t+.05))
    row=ws_split_ticker.make_split_ticker('BTCUSDT',(100.,now,'a'*32,round((now-20)*1000),1,1))
    result,_=ws_split_ticker.project_ticker(row)
    assert result==row
    row['_bcc_ws_mark_explicit_at']=round((now+1)*1000)
    assert ws_split_ticker.project_ticker(row)==(None,0.)
    with pytest.raises(DataUnavailable):PublicData._decode_ws_position_mark('BTCUSDT',row)


def test_unparseable_primary_frame_retires_exact_socket_only(monkeypatch):
    now=[round(time.time(),3)]
    monkeypatch.setattr(market_clock,'book_age_bounds',lambda t:(now[0]-t,now[0]-t+.05))
    notifications=[]
    stream=DualBybitQuoteStream(wall=lambda:now[0],monotonic=lambda:now[0],
        maintained=True,on_update=lambda *a:notifications.append(a))
    class OwnedSocket(Sink):
        def __init__(self):self.closed=False
        def close(self):self.closed=True
    primary=OwnedSocket(); reserve=OwnedSocket()
    stream.sync_symbols(['BTCUSDT'])
    generation=stream._primary.connect(primary)
    stream._recovery.connect(reserve)
    assert stream._primary.ingest(dict(type='snapshot',topic='tickers.BTCUSDT',
        ts=round(now[0]*1000),cs=1,data=dict(symbol='BTCUSDT',markPrice='100')),
        generation=generation)
    stream._primary._reject_wire(primary,generation-1)
    assert not primary.closed and stream._primary.maintained_mark('BTCUSDT') is not None
    notifications.clear()
    stream._primary._reject_wire(primary,generation)
    assert primary.closed and not reserve.closed
    assert stream._primary.maintained_mark('BTCUSDT') is None
    assert notifications==[('tickers','BTCUSDT')]
    stream.stop()
