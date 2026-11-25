from bcc.ws_ticker_state import MarkState
from tools.bybit_ticker_state_proof import sample, rest_comparison


def test_sample_does_not_mix_maintained_and_explicit_result():
    class Tracker:
        def comparison_observations(self):
            return ({'BTCUSDT':None, 'ETHUSDT':MarkState(200.,100000,100000,5,0.,'generation')},
                    {'BTCUSDT':MarkState(100.,9000,100000,5,0.,'generation'),
                     'ETHUSDT':MarkState(200.,100000,100000,5,0.,'generation')})
    result=sample(Tracker(),['BTCUSDT','ETHUSDT'],{'BTCUSDT'},lambda t:(100.1-t,100.2-t))
    assert result['explicit_pass']==0
    assert result['maintained_pass']==1
    assert result['rows'][0]['explicit_ms']==9000
    assert result['rows'][1]['maintained_fresh'] is False


def test_rest_compare_requires_same_origin_and_unchanged_explicit_stamp():
    a=MarkState(100.,9000,10000,1,0.,'a')
    b=MarkState(100.,9000,10200,2,.2,'a')
    c=MarkState(200.,9000,10200,2,.2,'b')
    payload=dict(retCode=0,time=10100,result=dict(category='linear',list=[
        dict(symbol='BTCUSDT',markPrice='100'),dict(symbol='ETHUSDT',markPrice='200')]))
    before={'BTCUSDT':a,'ETHUSDT':a};after={'BTCUSDT':b,'ETHUSDT':c}
    result=rest_comparison(before,after,payload,list(before))
    assert result['matched']==['BTCUSDT']
    assert result['uncomparable']==['ETHUSDT']
    payload['result']['list'][0]['markPrice']='101'
    assert rest_comparison(before,after,payload,list(before))['mismatched']==['BTCUSDT']


def test_rest_compare_does_not_bridge_new_snapshot_chain():
    a=MarkState(100.,9000,10000,1,0.,'a',1)
    b=MarkState(100.,9000,10200,2,.2,'a',2)
    payload=dict(retCode=0,time=10100,result=dict(category='linear',list=[dict(symbol='BTCUSDT',markPrice='100')]))
    result=rest_comparison({'BTCUSDT':a},{'BTCUSDT':b},payload,['BTCUSDT'])
    assert result['uncomparable']==['BTCUSDT']
    assert not result['matched']


def test_explicit_metric_counts_fresh_explicit_frame_after_broken_chain():
    from bcc.ws_ticker_state import TickerState
    tracker=TickerState(['BTCUSDT'],age_bounds=lambda _: (.1,.2),monotonic=lambda:1.)
    gen=tracker.connect()
    assert tracker.ingest(dict(topic='tickers.BTCUSDT',type='snapshot',cs=5,ts=1000,data=dict(symbol='BTCUSDT',markPrice='100')),gen)
    assert not tracker.ingest(dict(topic='tickers.BTCUSDT',type='delta',cs=4,ts=1100,data=dict(symbol='BTCUSDT',markPrice='101')),gen)
    row=sample(tracker,['BTCUSDT'],{'BTCUSDT'},lambda _:(.1,.2))
    assert row['explicit_pass']==1 and row['maintained_pass']==0
    assert row['rows'][0]['raw_explicit_ms']==1100
    assert 'confirmed_ms' not in row['rows'][0]


def test_full_owned_proof_recovers_broken_chain_without_counting_ack_as_price(tmp_path,monkeypatch):
    import json
    import queue
    import time
    import websocket
    from tools import bybit_ticker_state_proof as proof

    class Provider:
        def __init__(self):
            self.q=queue.Queue();self.commands=[];self.closed=False;self.subscriptions=0
        def settimeout(self,timeout):pass
        def send(self,raw):
            msg=json.loads(raw);self.commands.append(msg)
            op=msg['op']
            if op not in ('subscribe','unsubscribe'):return
            self.q.put(json.dumps(dict(op=op,req_id=msg['req_id'],success=True)))
            if op=='subscribe':
                self.subscriptions+=1
                stamp=int(time.time()*1000)
                self.q.put(json.dumps(dict(topic='tickers.BTCUSDT',type='snapshot',
                    ts=stamp,cs=10,data=dict(symbol='BTCUSDT',markPrice='100'))))
                if self.subscriptions==1:
                    self.q.put(json.dumps(dict(topic='tickers.BTCUSDT',type='delta',
                        ts=stamp+1,cs=9,data=dict(symbol='BTCUSDT',markPrice='101'))))
        def recv(self):
            try:return self.q.get(timeout=.005)
            except queue.Empty:raise websocket.WebSocketTimeoutException()
        def close(self):self.closed=True

    provider=Provider()
    monkeypatch.setattr(websocket,'create_connection',lambda *a,**k:provider)
    monkeypatch.setattr(proof.clock,'ensure',lambda:None)
    monkeypatch.setattr(proof.clock,'bounds',lambda:None)
    monkeypatch.setattr(proof,'book_age_bounds',lambda stamp:(time.time()-stamp,time.time()-stamp+.01))
    output=tmp_path/'proof.json'
    result=proof.run(['BTCUSDT'],output,round_count=2,interval=.03,warmup_seconds=.15)
    assert result['status']=='MAINTAINED_STATE_OBSERVATION_COMPLETE',result
    assert result['explicit_total']==2 and result['maintained_total']==0
    assert result['recovery_maintained_total']==2
    assert not result['baseline_complete'] and result['recovery_complete']
    assert result['baseline_stats']['first_breaks']['BTCUSDT']['reason']=='sequence_regression'
    assert result['recovery']['acknowledged_resubscriptions']==1
    assert len(result['recovery']['restored_snapshots'])==1
    assert result['owned_worker_retired'] and provider.closed
    assert [m['op'] for m in provider.commands]==['subscribe','unsubscribe','subscribe']
    assert not result['release_ready'] and not result['risk_decoder_changed']
