"""Deterministic replay of Bybit snapshot+delta; separate evidence clocks."""
import pytest
from bcc.ws_ticker_state import TickerState


class Replay:
    def __init__(self, names=('BTCUSDT', 'ETHUSDT')):
        self.now = 10000.
        self.tracker = TickerState(names, monotonic=lambda: self.now,
            age_bounds=lambda stamp: (self.now-stamp, self.now-stamp+.05))
        self.gen = self.tracker.connect()

    def feed(self, cs, *, symbol='BTCUSDT', kind='delta', stamp=None, **fields):
        return self.tracker.ingest(dict(topic='tickers.'+symbol, type=kind,
            ts=round(self.now*1000) if stamp is None else stamp, cs=cs,
            data=dict(symbol=symbol, **fields)), self.gen)

    def state(self, symbol='BTCUSDT'):
        return self.tracker.observations()[symbol]


def test_same_topic_omission_confirms_state_without_restamping_explicit_mark():
    r = Replay()
    assert r.feed(20, kind='snapshot', markPrice='100')
    original = r.state().explicit_ms
    for n in range(1, 92):
        r.now += 1.
        assert r.feed(20+n*17, lastPrice='102')
        assert r.state().price == 100.
        assert r.state().explicit_ms == original
        assert r.state().confirmed_ms == round(r.now*1000)
    assert r.tracker.stats()['omitted_mark_deltas'] == 91
    assert r.tracker.stats()['cross_sequence_jumps'] == 91
    assert r.feed(2000, markPrice='101')
    assert r.state().explicit_ms == r.state().confirmed_ms


def test_ack_other_symbol_and_silence_never_confirm():
    r = Replay()
    assert r.feed(1, kind='snapshot', markPrice='100')
    r.now += 3.1
    assert r.feed(2, symbol='ETHUSDT', kind='snapshot', markPrice='200')
    assert not r.tracker.ingest({'op':'pong'}, r.gen)
    assert r.state() is None
    assert r.feed(3, lastPrice='102')  # fresh same-topic evidence after silence
    assert r.state().price == 100.
    assert r.state().explicit_ms == 10000000
    assert r.feed(4, kind='snapshot', markPrice='101')
    assert r.state().price == 101.


@pytest.mark.parametrize('bad', [
    {'cs':None}, {'cs':True}, {'cs':0}, {'ts':9990000},
    {'data':{'symbol':'ETHUSDT','lastPrice':'100'}},
    {'data':{'symbol':'BTCUSDT','markPrice':'NaN'}},
    {'data':{'symbol':'BTCUSDT','lastPrice':{'invalid':True}}},
    {'data':{'symbol':'BTCUSDT'}}, {'type':'unknown'},
    {'data':{'symbol':'BTCUSDT','unknownField':'value'}},
])
def test_bad_frame_invalidates_until_new_snapshot(bad):
    r = Replay(); assert r.feed(10, kind='snapshot', markPrice='100')
    r.now += .1
    frame = dict(topic='tickers.BTCUSDT', type='delta', cs=11,
                 ts=round(r.now*1000), data=dict(symbol='BTCUSDT', lastPrice='101'))
    frame.update(bad)
    assert not r.tracker.ingest(frame, r.gen)
    assert r.state() is None
    assert not r.feed(12, markPrice='102')
    assert r.feed(13, kind='snapshot', markPrice='103')
    assert r.state().price == 103.


def test_duplicate_cannot_extend_confirmation_and_conflict_breaks_chain():
    r = Replay(); assert r.feed(10, kind='snapshot', markPrice='100')
    stamp = r.state().confirmed_ms
    r.now += 1.
    assert not r.feed(10, stamp=stamp, lastPrice='100')
    assert r.state().confirmed_ms == stamp
    assert not r.feed(10, stamp=stamp, markPrice='101')
    assert r.state() is None


def test_reconnect_cannot_reuse_old_generation_or_start_from_delta():
    r = Replay(); assert r.feed(10, kind='snapshot', markPrice='100')
    old = r.gen
    r.tracker.disconnect(old)
    assert r.state() is None
    r.gen = r.tracker.connect()
    r.tracker.disconnect(old)
    assert not r.feed(11, markPrice='101')
    assert r.feed(1, kind='snapshot', markPrice='102')
    assert not r.tracker.ingest({'topic':'tickers.BTCUSDT','type':'snapshot',
        'ts':round(r.now*1000),'cs':200,'data':{'symbol':'BTCUSDT','markPrice':'999'}},old)
    assert r.state().price == 102.


def test_invalid_new_snapshot_never_carries_old_mark():
    r = Replay(); assert r.feed(10, kind='snapshot', markPrice='100')
    r.now += .1
    assert not r.feed(11, kind='snapshot', lastPrice='101')
    assert r.state() is None


def test_500_symbols_24_rounds_with_unchanged_mark_and_real_format_deltas():
    names = tuple(f'Q{i}USDT' for i in range(500))
    r = Replay(names)
    for name in names:
        assert r.feed(1, symbol=name, kind='snapshot', markPrice='100')
    original = round(r.now*1000)
    for turn in range(24):
        r.now += 2.
        for name in names:
            assert r.feed(2+turn*19, symbol=name, lastPrice='101')
        states = r.tracker.observations()
        assert len(states) == 500
        assert all(s is not None and s.explicit_ms == original
                   and s.confirmed_ms == round(r.now*1000) for s in states.values())
    assert r.tracker.stats()['omitted_mark_deltas'] == 12000


def test_independent_explicit_measure_survives_chain_loss_without_repairing_it():
    r=Replay(); assert r.feed(10,kind='snapshot',markPrice='100')
    r.now+=.2
    assert not r.feed(9,markPrice='101')
    explicit,maintained=r.tracker.comparison_observations()
    assert maintained['BTCUSDT'] is None and explicit['BTCUSDT'].price==101
    first=r.tracker.stats()['first_breaks']['BTCUSDT']
    assert first['reason']=='sequence_regression' and first['previous_cs']==10
    assert first['current']['cs']==9 and first['current']['ts']==round(r.now*1000)
    r.now+=.2
    assert not r.feed(11,markPrice='102')
    assert r.tracker.comparison_observations()[0]['BTCUSDT'].price==102
    assert r.tracker.stats()['first_breaks']['BTCUSDT']==first
    assert r.feed(12,kind='snapshot',markPrice='103')
    assert r.state().price==103


@pytest.mark.parametrize('cs,offset,reason',[(9,.2,'sequence_regression'),(11,-.1,'timestamp_regression')])
def test_first_break_preserves_actual_comparison_evidence(cs,offset,reason):
    r=Replay(); assert r.feed(10,kind='snapshot',markPrice='100')
    old=round(r.now*1000);r.now+=max(offset,.1)
    assert not r.feed(cs,stamp=old+round(offset*1000),lastPrice='101')
    ev=r.tracker.stats()['first_breaks']['BTCUSDT']
    assert ev['reason']==reason and ev['previous_ts']==old
    assert ev['current']['ts']==old+round(offset*1000)
    assert ev['current']['fields']==['lastPrice']


def test_topic_reset_excludes_old_snapshot_and_requires_new_one():
    r=Replay();assert r.feed(10,kind='snapshot',markPrice='100')
    stamp=r.state().confirmed_ms
    assert r.tracker.reset_topics(['BTCUSDT'],r.gen)
    assert not r.feed(10,kind='snapshot',stamp=stamp,markPrice='100')
    assert r.tracker.comparison_observations()[0]['BTCUSDT'] is None
    r.now+=.2
    assert not r.feed(11,markPrice='101')
    assert r.state() is None
    r.now+=.2
    assert r.feed(1,kind='snapshot',markPrice='102')
    assert r.state().price==102


def test_duplicate_conflict_cannot_be_replayed_into_explicit_measure():
    r=Replay();assert r.feed(10,kind='snapshot',markPrice='100')
    assert not r.feed(10,markPrice='101')
    assert r.tracker.comparison_observations()[0]['BTCUSDT'] is None
    assert not r.feed(10,markPrice='100')
    assert r.tracker.comparison_observations()[0]['BTCUSDT'] is None
    r.now+=.1
    assert not r.feed(11,markPrice='102')
    assert r.tracker.comparison_observations()[0]['BTCUSDT'].price==102
    r.tracker.disconnect(r.gen)
    assert all(s is None for v in r.tracker.comparison_observations() for s in v.values())


@pytest.mark.parametrize('gap',[.2,3.2,15.,61.2])
@pytest.mark.parametrize('explicit',[False,True])
def test_same_sequence_can_resume_but_never_refresh_explicit_on_omission(gap,explicit):
    r=Replay();assert r.feed(10,kind='snapshot',markPrice='100')
    start=r.state();r.now+=gap
    if gap>3.:
        assert r.state() is None
        assert r.tracker.comparison_observations()[0]['BTCUSDT'] is None
    assert r.feed(10,**({'markPrice':'101'} if explicit else {'indexPrice':'101'}))
    end=r.state();assert end.chain==start.chain
    assert end.confirmed_ms==round(r.now*1000)
    assert end.explicit_ms==(end.confirmed_ms if explicit else start.explicit_ms)
    assert end.price==(101. if explicit else 100.)
    assert r.tracker.stats()['equal_sequence_new_timestamp']==1
    assert r.tracker.stats()['quiet_resumptions']==int(gap>3.)
    assert r.tracker.stats()['continuity_breaks']==0


def test_sparse_500_topics_24_rounds_do_not_fake_freshness_or_destroy_state():
    names=tuple(f'Q{i}USDT' for i in range(500));r=Replay(names)
    for s in names:assert r.feed(1,symbol=s,kind='snapshot',markPrice='100')
    initial=round(r.now*1000)
    for rnd in range(24):
        r.now+=4.+rnd
        assert all(v is None for v in r.tracker.observations().values())
        for i,s in enumerate(names):
            if i%2==rnd%2:
                assert r.feed(1,symbol=s,indexPrice='101')
        states=r.tracker.observations()
        assert sum(v is not None for v in states.values())==250
        assert all(v is None or v.explicit_ms==initial for v in states.values())
        assert all(v is None for v in r.tracker.comparison_observations()[0].values())
    stats=r.tracker.stats()
    assert stats['quiet_omitted_resumptions']==6000
    assert stats['continuity_breaks']==0
