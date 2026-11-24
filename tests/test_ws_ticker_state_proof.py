from bcc.ws_ticker_state import MarkState
from tools.bybit_ticker_state_proof import sample, rest_comparison


def test_sample_does_not_mix_maintained_and_explicit_result():
    class Tracker:
        def observations(self):
            return {'BTCUSDT':MarkState(100.,9000,100000,5,0.,'generation'),
                    'ETHUSDT':MarkState(200.,100000,100000,5,0.,'generation')}
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
