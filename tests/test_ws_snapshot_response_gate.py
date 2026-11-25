"""Test actual stream recovery under delayed, partial and replayed WS frames."""
import json
import pytest

from bcc.ws_quotes import BybitQuoteStream, StreamUnavailable
from bcc.ws_orderbooks import BybitOrderbookWS
from bcc.ws_recovery import SnapshotRecovery, SNAPSHOT_RESPONSE_TIMEOUT


class Sink:
    def __init__(self):
        self.messages = []

    def send(self, data):
        self.messages.append(json.loads(data))

    def close(self):
        pass


def ticker_frame(stream, gen, now, *, symbol='BTCUSDT', kind='snapshot', **fields):
    return stream.ingest({'type':kind, 'topic':'tickers.'+symbol,
                          'ts':int(now*1000), 'data':fields}, generation=gen)


def book_frame(stream, gen, now, *, symbol='BTCUSDT', seq=1, kind='snapshot'):
    return stream.ingest({'type':kind, 'topic':'orderbook.50.'+symbol,
                          'ts':int(now*1000), 'data':{'s':symbol,'u':seq,'seq':seq,
                          'b':[['100','1']], 'a':[['101','1']]}}, generation=gen)


def test_waiting_requires_strictly_new_evidence_not_partial_or_replayed():
    gate = SnapshotRecovery()
    gate.begin('BTCUSDT', 10., (4,5,6))
    assert not gate.observe_snapshot('BTCUSDT', (7,8))
    assert not gate.observe_snapshot('BTCUSDT', (7,5,8))
    assert gate.waiting('BTCUSDT', 11.)
    assert gate.observe_snapshot('BTCUSDT', (7,8,9))
    assert not gate.waiting('BTCUSDT', 11.1)
    assert gate.stats()=={'pending_snapshot_topics':0,'recovered_snapshot_topics':1,
                          'snapshot_response_timeouts':0}
    gate.begin('BTCUSDT', 12., (7,8,9))
    assert not gate.waiting('BTCUSDT', 12.+SNAPSHOT_RESPONSE_TIMEOUT)
    assert gate.stats()['snapshot_response_timeouts']==1


def test_ticker_partial_and_duplicate_frames_do_not_end_pending_recovery():
    now=[10000.]; sock=Sink()
    stream=BybitQuoteStream(wall=lambda:now[0],monotonic=lambda:now[0])
    stream.subscribe('BTCUSDT'); gen=stream.connect(sock)
    stream.set_priority_symbols(['BTCUSDT'])
    fields=dict(markPrice='100.5',bid1Price='100',ask1Price='101')
    assert ticker_frame(stream,gen,now[0],**fields)
    now[0]+=.6
    assert stream.refresh_stale_marks(['BTCUSDT'])==1
    # Replaying the original snapshot or seeing only mark is not completion.
    ticker_frame(stream,gen,10000.,**fields)
    now[0]+=.6
    assert ticker_frame(stream,gen,now[0],kind='delta',markPrice='100.6')
    assert stream.refresh_stale_marks(['BTCUSDT'])==0
    assert stream.refresh_pressure()['pending_snapshot_topics']==1
    assert len(sock.messages)==2
    now[0]+=.1
    assert ticker_frame(stream,gen,now[0],**fields)
    assert stream.refresh_pressure()['pending_snapshot_topics']==0
    assert stream.refresh_pressure()['recovered_snapshot_topics']==1
    assert stream.mark('BTCUSDT')[1]==pytest.approx(int(now[0]*1000)/1000)


def test_ticker_unanswered_request_does_not_extend_old_price_authority():
    now=[10000.]; sock=Sink()
    stream=BybitQuoteStream(wall=lambda:now[0],monotonic=lambda:now[0])
    stream.subscribe('BTCUSDT'); gen=stream.connect(sock)
    stream.set_priority_symbols(['BTCUSDT'])
    assert ticker_frame(stream,gen,now[0],markPrice='100.5',bid1Price='100',ask1Price='101')
    now[0]+=.6
    assert stream.refresh_stale_marks(['BTCUSDT'])==1
    now[0]+=.51
    assert stream.refresh_stale_marks(['BTCUSDT'])==0
    now[0]+=.75
    assert stream.refresh_stale_marks(['BTCUSDT'])==1
    assert stream.refresh_pressure()['snapshot_response_timeouts']==1
    now[0]+=.2
    with pytest.raises(StreamUnavailable):
        stream.mark('BTCUSDT')
    with pytest.raises(StreamUnavailable):
        stream.quote('BTCUSDT')
    stream.disconnect()
    assert stream.refresh_pressure()['pending_snapshot_topics']==0
    assert not ticker_frame(stream,gen,now[0],markPrice='100.5',bid1Price='100',ask1Price='101')


def test_depth_delta_or_replayed_snapshot_cannot_clear_pending_but_new_snapshot_can():
    now=[10000.]; sock=Sink()
    stream=BybitOrderbookWS(wall=lambda:now[0],monotonic=lambda:now[0])
    stream.subscribe(['BTCUSDT']); gen=stream.connect(sock)
    assert book_frame(stream,gen,now[0])
    now[0]+=1.1
    assert stream.refresh_stale_books(['BTCUSDT'])==1
    assert book_frame(stream,gen,10000.)
    assert stream.refresh_pressure()['pending_snapshot_topics']==1
    now[0]+=.2
    assert book_frame(stream,gen,now[0],seq=2,kind='delta')
    assert stream.refresh_pressure()['pending_snapshot_topics']==1
    now[0]+=.1
    assert book_frame(stream,gen,now[0],seq=3)
    assert stream.refresh_pressure()['pending_snapshot_topics']==0
    assert stream.refresh_pressure()['recovered_snapshot_topics']==1


@pytest.mark.parametrize('factory,subscribe,refresh', [
    (BybitQuoteStream,'subscribe_many','refresh_stale_marks'),
    (BybitOrderbookWS,'subscribe','refresh_stale_books'),
])
def test_pending_state_removed_with_subscription_and_socket_generation(factory,subscribe,refresh):
    now=[10000.]; sock=Sink(); stream=factory(wall=lambda:now[0],monotonic=lambda:now[0])
    names=['BTCUSDT','ETHUSDT']; getattr(stream,subscribe)(names)
    stream.connect(sock); stream.set_priority_symbols(names)
    now[0]+=3.1
    assert getattr(stream,refresh)(names)==2
    stream.sync_symbols(['BTCUSDT'])
    assert stream.refresh_pressure()['pending_snapshot_topics']==1
    stream.connect(Sink())
    assert stream.refresh_pressure()['pending_snapshot_topics']==0


@pytest.mark.parametrize('factory,subscribe,refresh', [
    (BybitQuoteStream,'subscribe_many','refresh_stale_marks'),
    (BybitOrderbookWS,'subscribe','refresh_stale_books'),
])
def test_500_silent_symbols_24_scans_no_overlap_or_starvation(factory,subscribe,refresh):
    now=[10000.]; sock=Sink(); stream=factory(wall=lambda:now[0],monotonic=lambda:now[0])
    names=[f'C{i}USDT' for i in range(500)]
    getattr(stream,subscribe)(names); stream.set_priority_symbols(names)
    stream.connect(sock); now[0]+=3.1
    last={}
    for _ in range(24):
        before=len(sock.messages)
        count=getattr(stream,refresh)(names)
        assert count<=128
        for msg in sock.messages[before:]:
            if msg['op']=='unsubscribe':
                for topic in msg['args']:
                    if topic in last:
                        assert now[0]-last[topic]>=SNAPSHOT_RESPONSE_TIMEOUT-1e-8
                    last[topic]=now[0]
        now[0]+=.25
    assert len(last)==500
    assert stream.refresh_pressure()['pending_snapshot_topics']<=500
    assert stream.refresh_pressure()['recovered_snapshot_topics']==0
