import pytest
from bcc.ws_maintained_marks import MaintainedMarks, maintained_metadata


class Replay:
    def __init__(self, symbols=('BTCUSDT', 'ETHUSDT')):
        self.now = 10000.
        self.fence = 'a' * 32
        self.state = MaintainedMarks(age_bounds=lambda t: (self.now-t,self.now-t+.05),
            monotonic=lambda: self.now, utc_upper=lambda: self.now+.05)
        self.state.reset(self.fence, symbols)

    def feed(self, cs, symbol='BTCUSDT', kind='delta', stamp=None, fence=None, **fields):
        return self.state.ingest(dict(topic='tickers.'+symbol, type=kind, cs=cs,
            ts=round(self.now*1000) if stamp is None else stamp,
            data=dict(symbol=symbol, **fields)), self.fence if fence is None else fence)

    def mark(self, symbol='BTCUSDT'):
        return self.state.mark(symbol)


def test_explicit_and_confirmation_clocks_survive_quiet_resume():
    r = Replay()
    assert not r.feed(1, markPrice='100')
    assert r.feed(1, kind='snapshot', markPrice='100')
    original = r.mark()
    r.now += 14.
    assert r.mark() is None
    assert r.feed(1, 'ETHUSDT', kind='snapshot', markPrice='200')
    assert not r.state.ingest({'op':'pong'}, r.fence)
    assert r.mark() is None
    assert r.feed(1, indexPrice='101')
    resumed = r.mark()
    assert resumed == (100., r.now, r.fence, original[3], 1, original[5])
    r.now += .1
    assert r.feed(2, markPrice='103')
    assert r.mark()[:4] == (103., r.now, r.fence, round(r.now*1000))


@pytest.mark.parametrize('bad', ['regression','timestamp','conflict','malformed','wrong_symbol','missing_mark_snapshot'])
def test_corruption_requires_new_snapshot(bad):
    r=Replay();assert r.feed(10,kind='snapshot',markPrice='100')
    old=round(r.now*1000);r.now+=.2
    if bad=='regression': r.feed(9,markPrice='101')
    elif bad=='timestamp': r.feed(11,stamp=old-1,lastPrice='102')
    elif bad=='conflict': r.feed(11,stamp=old,markPrice='101')
    elif bad=='malformed': r.feed(11,markPrice='NaN')
    elif bad=='missing_mark_snapshot': r.feed(11,kind='snapshot',lastPrice='101')
    else: r.state.ingest(dict(topic='tickers.BTCUSDT',type='delta',ts=round(r.now*1000),cs=11,data=dict(symbol='ETHUSDT',markPrice='101')),r.fence)
    assert r.mark() is None
    r.now+=.1;assert not r.feed(12,markPrice='103');assert r.mark() is None
    r.now+=.1;assert r.feed(13,kind='snapshot',markPrice='104');assert r.mark()[0]==104.


def test_remove_readd_and_reconnect_never_inherit_old_state():
    r=Replay();assert r.feed(1,kind='snapshot',markPrice='100')
    r.state.sync(['ETHUSDT']);r.state.sync(['ETHUSDT','BTCUSDT'])
    assert not r.feed(1,kind='snapshot',markPrice='999');assert r.mark() is None
    r.now+=.2;assert not r.feed(2,markPrice='999')
    assert r.feed(2,kind='snapshot',markPrice='101')
    r.state.reset('b'*32,['BTCUSDT']);assert r.mark() is None
    assert not r.feed(3,kind='snapshot',markPrice='999')
    assert not r.feed(3,fence='b'*32,markPrice='999')
    assert r.feed(1,fence='b'*32,kind='snapshot',markPrice='102')
    assert r.mark()[2]=='b'*32


def test_duplicate_does_not_renew_age():
    r=Replay();assert r.feed(1,kind='snapshot',markPrice='100');stamp=round(r.now*1000)
    r.now+=2.;assert not r.feed(1,stamp=stamp,indexPrice='100')
    r.now+=1.1;assert r.mark() is None


@pytest.mark.parametrize('field,bad', [('_bcc_ws_mark_explicit_at',1001),('_bcc_ws_mark_explicit_at',True),('_bcc_ws_mark_cs',-1),('_bcc_ws_mark_chain',0),('_bcc_ws_mark_basis','rest'),('_bcc_ws_split_ticker',False)])
def test_bad_provenance_rejected(field,bad):
    v=dict(_bcc_ws_mark_at=1000,_bcc_ws_mark_explicit_at=900,_bcc_ws_mark_cs=2,
        _bcc_ws_mark_chain=1,_bcc_ws_mark_basis='snapshot_delta',_bcc_ws_split_ticker=True)
    assert maintained_metadata(v)==(900,2,1)
    v[field]=bad
    with pytest.raises(ValueError):maintained_metadata(v)


def test_500_topics_24_rounds_with_unchanged_marks_and_quiet_topics():
    names=[f'Q{i}USDT' for i in range(500)];r=Replay(names)
    for s in names:assert r.feed(1,s,kind='snapshot',markPrice='100')
    original=round(r.now*1000)
    for n in range(24):
        r.now+=4.
        assert all(r.mark(s) is None for s in names)
        for i,s in enumerate(names):
            if i%2==n%2:assert r.feed(1,s,indexPrice='102')
        assert r.state.stats()['fresh']==250
        assert all(r.mark(s) is None or r.mark(s)[3]==original for s in names)
