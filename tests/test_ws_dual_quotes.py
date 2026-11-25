"""A stable ticker primary and independent bounded recovery; no real network."""
import json
import threading

import pytest

from bcc.ws_dual_quotes import DualBybitQuoteStream
from bcc.ws_quotes import StreamUnavailable


class Socket:
    def __init__(self):
        self.messages = []
        self.closed = False

    def send(self, raw):
        self.messages.append(json.loads(raw))

    def close(self):
        self.closed = True


def setup(names=('BTCUSDT',)):
    now = [10000.]
    stream = DualBybitQuoteStream(wall=lambda: now[0], monotonic=lambda: now[0])
    primary, recovery = Socket(), Socket()
    stream._primary.connect(primary)
    stream._recovery.connect(recovery)
    stream.sync_symbols(list(names))
    stream.set_priority_symbols(list(names))
    return now, stream, primary, recovery


def frame(child, now, symbol='BTCUSDT', kind='snapshot', **data):
    return child.ingest(dict(type=kind, topic='tickers.'+symbol,
                             ts=int(now*1000), data=data), generation=child._generation)


def full(child, now, symbol='BTCUSDT', mark='100.5'):
    return frame(child, now, symbol, markPrice=mark,bid1Price='100',ask1Price='101')


def test_stable_primary_never_resubscribed_for_quiet_mark():
    now, stream, primary, recovery = setup()
    assert full(stream._primary,now[0])
    assert stream.refresh_stale_marks(['BTCUSDT'])==0
    now[0]+=1.1
    assert stream.refresh_stale_marks(['BTCUSDT'])==1
    assert primary.messages==[{'op':'subscribe','args':['tickers.BTCUSDT']}]
    assert recovery.messages==primary.messages
    assert full(stream._recovery,now[0],mark='101.5')
    assert stream.mark('BTCUSDT')==(101.5,int(now[0]*1000)/1000)
    now[0]+=1.1
    stream.refresh_stale_marks(['BTCUSDT'])
    assert primary.messages==[{'op':'subscribe','args':['tickers.BTCUSDT']}]
    assert stream.refresh_pressure()['primary']['snapshot_topics_requested']==0
    assert len(recovery.messages)>1


def test_cross_socket_marks_and_quotes_are_never_merged_into_a_publication():
    now,stream,_,_=setup()
    assert full(stream._primary,now[0])
    now[0]+=1.1
    assert stream.refresh_stale_marks(['BTCUSDT'])==1
    assert frame(stream._recovery,now[0],markPrice='104')
    mark, quote, fence=stream.snapshot_for_hub('BTCUSDT')
    assert mark[0]==104
    assert quote is None
    assert fence==stream._recovery._connection_fence
    assert fence!=stream._primary._connection_fence
    assert stream.quote('BTCUSDT')[:2]==(100.,101.)
    now[0]+=.1
    assert full(stream._primary,now[0],mark='105')
    mark,quote,fence=stream.snapshot_for_hub('BTCUSDT')
    assert mark[0]==105 and quote is not None
    assert fence==stream._primary._connection_fence


def test_disconnect_removal_and_expiry_do_not_restore_old_prices():
    now,stream,_,_=setup()
    assert full(stream._primary,now[0])
    now[0]+=1.1
    stream.refresh_stale_marks(['BTCUSDT'])
    assert full(stream._recovery,now[0],mark='102')
    stream._recovery.disconnect()
    assert stream.mark('BTCUSDT')[0]==100.5
    now[0]+=1.1
    with pytest.raises(StreamUnavailable):
        stream.mark('BTCUSDT')
    with pytest.raises(StreamUnavailable):
        stream.quote('BTCUSDT')
    stream.sync_symbols([])
    assert stream.snapshot_for_hub('BTCUSDT')==(None,None,None)
    assert stream.transport_health()['primary']['subscribed']==0
    assert stream.transport_health()['recovery']['subscribed']==0


def ack_commands(child,socket,start):
    # A deterministic fake provider confirms each correlated wire command,
    # but sends NO market observations. ACKs alone never supply a price.
    index=start
    while index < len(socket.messages):
        msg=socket.messages[index];index+=1
        if 'req_id' in msg:
            assert child._handle_control_message(socket,
                dict(req_id=msg['req_id'],op=msg['op'],success=True),child._generation)


def test_500_quiet_symbols_24_scans_only_shadow_can_recover():
    names=[f'C{i}USDT' for i in range(500)]
    now,stream,primary,recovery=setup(names)
    initial=list(primary.messages)
    for _ in range(24):
        before=len(recovery.messages)
        admitted=stream.refresh_stale_marks(names)
        assert admitted<=128
        ack_commands(stream._recovery,recovery,before)
        messages=recovery.messages[before:]
        assert sum(len(m['args']) for m in messages if m['op']=='subscribe')<=128
        now[0]+=.25
    assert primary.messages==initial
    assert stream.transport_health()['recovery']['subscribed']==500
    assert stream.refresh_pressure()['primary']['snapshot_topics_requested']==0
    assert stream.refresh_pressure()['recovery']['snapshot_topics_requested']>0
    assert stream.snapshot_for_hub(names[0])==(None,None,None)


def test_new_shadow_demand_does_not_starve_background_or_existing_due():
    names=[f'C{i}USDT' for i in range(500)]
    now,stream,_,recovery=setup(names)
    stream.set_priority_symbols([])
    for _ in range(40):
        before=len(recovery.messages)
        stream.refresh_stale_marks(names)
        ack_commands(stream._recovery,recovery,before)
        now[0]+=.25
    assert stream.transport_health()['recovery']['subscribed']==500
    assert stream.refresh_pressure()['recovery']['snapshot_topics_requested']>0


def test_dual_child_shutdown_and_start_race():
    stream=DualBybitQuoteStream()
    stream.stop()
    # Once stopped, a delayed run must not open a provider connection.
    stream.run(websocket_app_factory=lambda *a,**k:pytest.fail('unexpected connection'))
    for child in (stream._primary,stream._recovery):
        child.run(websocket_app_factory=lambda *a,**k:pytest.fail('unexpected connection'),reset_stop=False)


def test_failed_child_retires_both_lanes_without_running_forever(monkeypatch):
    stream=DualBybitQuoteStream()
    stop=threading.Event()
    monkeypatch.setattr(stream._primary,'run',lambda **k: (_ for _ in ()).throw(RuntimeError('source failure')))
    monkeypatch.setattr(stream._recovery,'run',lambda **k:stop.wait(1))
    monkeypatch.setattr(stream._recovery,'stop',lambda:stop.set())
    with pytest.raises(RuntimeError,match='child failed'):
        stream.run()
    assert stop.is_set()
    assert stream._stop.is_set()


def test_shadow_requires_matching_unsubscribe_ack_before_subscribe():
    now,stream,primary,sock=setup()
    assert full(stream._primary,now[0])
    now[0]+=1.1
    stream.refresh_stale_marks(['BTCUSDT'])
    assert full(stream._recovery,now[0])
    now[0]+=.6
    assert stream.refresh_stale_marks(['BTCUSDT'])==0  # fresh reserve mark suffices
    now[0]+=.5
    assert stream.refresh_stale_marks(['BTCUSDT'])==1
    child=stream._recovery
    req=sock.messages[-1]
    assert req['op']=='unsubscribe'
    assert child.refresh_pressure()['pending_wire_topics']==1
    now[0]+=.6
    stream.refresh_stale_marks(['BTCUSDT'])
    assert sock.messages[-1]==req
    assert not child._handle_control_message(sock,dict(req_id='unrelated',op='unsubscribe',success=True),child._generation)
    assert sock.messages[-1]==req
    assert child._handle_control_message(sock,dict(req_id=req['req_id'],op='unsubscribe',success=True),child._generation)
    sub=sock.messages[-1]
    assert sub['op']=='subscribe' and sub['req_id']!=req['req_id']
    assert child._handle_control_message(sock,dict(req_id=sub['req_id'],op='subscribe',success=True),child._generation)
    assert child.refresh_pressure()['pending_wire_topics']==0
    assert child.refresh_pressure()['pending_snapshot_topics']==1
    now[0]+=.5
    assert stream.refresh_stale_marks(['BTCUSDT'])==0
    assert primary.messages==[{'op':'subscribe','args':['tickers.BTCUSDT']}]
    assert full(child,now[0])
    assert child.refresh_pressure()['pending_snapshot_topics']==0


def test_missing_ack_retires_shadow_only_and_never_creates_data():
    now,stream,primary,sock=setup()
    assert full(stream._primary,now[0])
    now[0]+=1.1
    stream.refresh_stale_marks(['BTCUSDT'])
    assert full(stream._recovery,now[0])
    now[0]+=.6
    assert stream.refresh_stale_marks(['BTCUSDT'])==0  # fresh reserve mark suffices
    now[0]+=.5
    assert stream.refresh_stale_marks(['BTCUSDT'])==1
    now[0]+=1.3
    with pytest.raises(StreamUnavailable,match='ACK timeout'):
        stream.refresh_stale_marks(['BTCUSDT'])
    assert sock.closed and not stream._recovery._connected
    assert stream._primary._connected and not primary.closed
    assert stream._recovery.refresh_pressure()['wire_timeouts']==1
    with pytest.raises(StreamUnavailable):
        stream.mark('BTCUSDT')
    assert full(stream._primary,now[0])
    assert stream.mark('BTCUSDT')[0]==100.5


def test_removed_symbol_cannot_be_resubscribed_by_delayed_ack():
    now,stream,primary,sock=setup()
    assert full(stream._primary,now[0])
    now[0]+=1.1
    stream.refresh_stale_marks(['BTCUSDT'])
    assert full(stream._recovery,now[0])
    now[0]+=.6
    assert stream.refresh_stale_marks(['BTCUSDT'])==0  # fresh reserve mark suffices
    now[0]+=.5
    stream.refresh_stale_marks(['BTCUSDT'])
    child=stream._recovery
    gen=child._generation;req=sock.messages[-1];n=len(sock.messages)
    stream.sync_symbols([])
    assert sock.closed
    assert child._handle_control_message(sock,dict(req_id=req['req_id'],op='unsubscribe',success=True),gen)
    assert len(sock.messages)==n
    assert stream.snapshot_for_hub('BTCUSDT')==(None,None,None)
    assert not primary.closed


def test_dual_candidate_publication_to_real_sqlite_and_reader(tmp_path, monkeypatch):
    import time
    from types import SimpleNamespace
    from bcc import data_cache, market_clock
    from bcc.market import PublicData
    from bcc.market_hub import MarketDataHub
    from bcc.ws_orderbooks import BybitOrderbookWS
    now=[time.time()]
    controlled=SimpleNamespace(time=lambda:now[0],monotonic=time.monotonic,sleep=time.sleep)
    monkeypatch.setattr(data_cache,'time',controlled)
    monkeypatch.setattr(market_clock,'time',controlled)
    reference=market_clock.MarketClock()
    monkeypatch.setattr(market_clock,'clock',reference)
    monkeypatch.setattr('bcc.market.clock',reference)
    stream=DualBybitQuoteStream(wall=lambda:now[0],monotonic=lambda:now[0])
    primary,recovery=Socket(),Socket()
    stream._primary.connect(primary);stream._recovery.connect(recovery)
    depth=BybitOrderbookWS(wall=lambda:now[0],monotonic=lambda:now[0])
    depth.connect(Socket())
    source=PublicData(quote_stream=stream,orderbook_stream=depth)
    path=tmp_path/'cache.sqlite'
    hub=MarketDataHub(path,source=source)
    reader=None
    try:
        assert hub.acquire_leadership()
        for url,params in [('https://api.bybit.com/v5/market/tickers',dict(category='linear',symbol='BTCUSDT')),
                           ('https://api.bybit.com/v5/market/orderbook',dict(category='linear',symbol='BTCUSDT',limit=50))]:
            hub.registry.subscribe(url,params,priority=True)
        hub.source.shared.bybit_ws_source_deadlines=True
        hub.subscribe_active_websockets()
        assert full(stream._primary,now[0])
        stamp=int(now[0]*1000)
        assert depth.ingest({'type':'snapshot','topic':'orderbook.50.BTCUSDT','ts':stamp,
            'data':{'s':'BTCUSDT','u':1,'seq':1,'b':[['100','1']],'a':[['101','1']]}},generation=depth._generation)
        assert hub.publish_ws_events([('tickers','BTCUSDT'),('depth50','BTCUSDT')])==[True,True]
        monkeypatch.setenv('BCC_PUBLIC_CACHE',str(path));monkeypatch.setenv('BCC_MARKET_HUB_MODE','client')
        reader=PublicData();reader.set_position_symbols(['BTCUSDT'])
        marks,quotes=reader._ws_portfolio_observations()
        assert marks['BTCUSDT']['at']==stamp/1000
        assert quotes['BTCUSDT']['at']==stamp/1000
        now[0]+=1.1
        stream.refresh_stale_marks(['BTCUSDT'])
        assert full(stream._recovery,now[0],mark='102')
        assert hub.publish_ws_event('tickers','BTCUSDT')
        reader._portfolio_ws_cache=None
        marks,quotes=reader._ws_portfolio_observations()
        assert marks['BTCUSDT']['bid']==102
        assert primary.messages==[{'op':'subscribe','args':['tickers.BTCUSDT']}]
        now[0]+=3.1
        reader._portfolio_ws_cache=None
        assert reader._ws_portfolio_observations()==({}, {})
    finally:
        if reader is not None:reader.close()
        hub.close();source.close();stream.stop();depth.stop()


def test_maintained_candidate_revokes_removed_pending_topic_without_resetting_siblings():
    now=[10000.]
    stream=DualBybitQuoteStream(wall=lambda:now[0],monotonic=lambda:now[0],maintained=True)
    primary,sock=Socket(),Socket()
    stream._primary.connect(primary);stream._recovery.connect(sock)
    names=['BTCUSDT','ETHUSDT']
    stream.sync_symbols(names);stream.set_priority_symbols(names)
    now[0]+=1.
    assert stream.refresh_stale_marks(names)==2
    child=stream._recovery
    assert full(child,now[0],'BTCUSDT') and full(child,now[0],'ETHUSDT')
    now[0]+=1.1
    assert stream.refresh_stale_marks(names)==2
    req=sock.messages[-1]
    assert req['op']=='unsubscribe'
    generation=child._generation
    stream.sync_symbols(['ETHUSDT'])
    assert not sock.closed and child._generation==generation
    assert stream.snapshot_for_hub('BTCUSDT')==(None,None,None)
    # Re-add demand while its old wire operation is unsettled. The
    # physically retained recovery topic cannot publish into the new demand.
    stream.sync_symbols(names)
    assert stream.independent_observations_for_hub('BTCUSDT')==(None,None)
    assert child._handle_control_message(sock,dict(req_id=req['req_id'],op='unsubscribe',success=True),generation)
    ack_commands(child,sock,len(sock.messages)-1)
    stream.sync_symbols(names)
    assert 'BTCUSDT' not in child._subscriptions
    assert 'ETHUSDT' in child._subscriptions
    assert not sock.closed and not primary.closed
    assert stream.independent_observations_for_hub('BTCUSDT')==(None,None)
