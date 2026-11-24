"""Replay the actual diagnostic recovery controller, never a live socket."""
import pytest
from bcc.ws_ticker_state import TickerState
from tools.ticker_proof_recovery import ProofRecovery
from tools.bybit_ticker_state_proof import sample


class Replay:
    def __init__(self, names=('BTCUSDT',)):
        self.now=10000.
        self.names=names
        self.tracker=TickerState(names,age_bounds=self.bounds,monotonic=lambda:self.now)
        self.gen=self.tracker.connect()
        self.sent=[];self.acked=set(names)
        self.recovery=ProofRecovery(self.tracker,self.gen,self.sent.append,self.acked,monotonic=lambda:self.now)

    def bounds(self,stamp):
        return self.now-stamp,self.now-stamp+.05

    def frame(self,symbol,cs,kind='snapshot',stamp=None):
        if not self.recovery.allows_frame(symbol):return False
        return self.tracker.ingest(dict(topic='tickers.'+symbol,cs=cs,type=kind,
            ts=round(self.now*1000) if stamp is None else stamp,data=dict(symbol=symbol,markPrice='100')),self.gen)

    def ack(self,**changes):
        msg=dict(self.sent[-1],success=True);msg.update(changes)
        return self.recovery.acknowledge(msg)


def test_actual_recovery_fences_frames_until_new_snapshot_and_ack():
    r=Replay();s=r.names[0]
    assert r.frame(s,10)
    old=round(r.now*1000)
    r.now+=.1;assert not r.frame(s,9,'delta')
    r.recovery.poll()
    assert len(r.sent)==1 and r.sent[-1]['op']=='unsubscribe' and not r.acked
    assert not r.frame(s,11)
    assert not r.ack(req_id='unrelated') and len(r.sent)==1
    assert r.ack() and r.sent[-1]['op']=='subscribe'
    assert not r.frame(s,10,stamp=old)
    r.now+=.1;assert r.frame(s,1)
    # Snapshot can arrive before its subscribe ACK, but is not counted yet.
    assert sample(r.tracker,r.names,r.acked,r.bounds)['maintained_pass']==0
    assert r.ack()
    assert sample(r.tracker,r.names,r.acked,r.bounds)['maintained_pass']==1
    assert r.recovery.stats()['acknowledged_resubscriptions']==1
    r.recovery.poll()
    assert r.recovery.stats()['restored_snapshots'][0]['symbol']==s
    assert len(r.sent)==2


def test_missing_or_wrong_ack_fails_closed():
    r=Replay();r.recovery.poll();r.now+=5.01
    with pytest.raises(RuntimeError,match='timeout'):r.recovery.poll()
    assert not r.acked
    r=Replay();r.recovery.poll()
    with pytest.raises(RuntimeError,match='invalid'):r.ack(success=False)
    assert not r.acked and len(r.sent)==1


def test_bounded_recovery_500_silent_symbols_no_unlimited_retries():
    names=tuple(f'Q{i}USDT' for i in range(500));r=Replay(names)
    for _ in range(101):
        before=len(r.sent);r.recovery.poll()
        if len(r.sent)>before:
            assert r.sent[-1]['op']=='unsubscribe'
            assert len(r.sent[-1]['args'])<=10
            assert r.ack() and r.ack()
            assert len(r.sent)==before+2
        r.now+=2.1
    assert set(r.recovery.attempts)==set(names)
    assert set(r.recovery.attempts.values())=={2}
    assert r.recovery.completed==1000
    assert r.recovery.pending is None and r.acked==set(names)
    assert sample(r.tracker,names,r.acked,r.bounds)['maintained_pass']==0
