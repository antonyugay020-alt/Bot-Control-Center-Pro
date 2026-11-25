"""Pure evidence-admission checks for the read-only public portfolio QA gate."""
import pytest
from tools.bybit_portfolio_gate import (assess_cohort, inspect_candles,
                                       inspect_tiers, status_from_evidence,
                                       validate_symbols)


def test_public_symbols_preserve_cardinality_and_order():
    assert validate_symbols(['BTCUSDT', '4USDT', 'DOGEUSDT']) == (
        'BTCUSDT', '4USDT', 'DOGEUSDT')
    assert len(validate_symbols([f'S{i}USDT' for i in range(12000)])) == 12000


@pytest.mark.parametrize('symbols', [[], {}, ['btcUSDT'], ['BTCUSDT','BTCUSDT'],
                                    ['BTC-USDT'], ['BTCUSDT',''], [34], ['BTCUSDT', None]])
def test_invalid_or_duplicate_symbols_rejected_not_silently_dropped(symbols):
    with pytest.raises(ValueError):
        validate_symbols(symbols)


def test_independent_original_tier_timestamp_required():
    assert inspect_tiers({'tiers':[{'riskLimitValue':'10'}], 'risk_at':999.},1000.) == 1.
    for bad in ({'tiers':[1]}, {'tiers':[], 'risk_at':1000},
                {'tiers':[1], 'risk_at':699.}, {'tiers':[1], 'risk_at':1001.},
                {'tiers':[1], 'risk_at':float('nan')}):
        with pytest.raises(ValueError):
            inspect_tiers(bad,1000.)


def test_multiplicity_and_continuity_of_real_candles():
    bars=[{'t':i*60000} for i in range(100)]
    assert inspect_candles(bars,100*60+20)==80.
    with pytest.raises(ValueError,match='Gapped'):
        inspect_candles(bars[:50]+[{'t':51*60000}]+bars[51:], 6020.)
    with pytest.raises(ValueError,match='Stale'):
        inspect_candles(bars, 100*60+121)
    with pytest.raises(ValueError,match='Incomplete'):
        inspect_candles(bars[:99],6020.)


def test_partial_portfolio_cannot_pass_on_other_symbols():
    cohort=assess_cohort(('BTCUSDT','ETHUSDT'), {'BTCUSDT':7},
                         {'BTCUSDT':8,'ETHUSDT':9},lambda value, maximum: value is not None)
    assert cohort == {'BTCUSDT': {'mark':True, 'executable_quote':True},
                      'ETHUSDT': {'mark':False, 'executable_quote':True}}
    rows={s:{'risk_status':'PASS','candles_status':'PASS','book_status':'PASS'} for s in cohort}
    assert status_from_evidence(rows,cohort,tuple(rows),cleanup=True,source_ok=True) == 'NOT_CONFIRMED'
    cohort['ETHUSDT']['mark']=True
    assert status_from_evidence(rows,cohort,tuple(rows),cleanup=True,source_ok=True) == 'PUBLIC_PORTFOLIO_DATA_PASS'
    assert status_from_evidence(rows,cohort,('BTCUSDT','ETHUSDT','DOGEUSDT'),cleanup=True,source_ok=True) == 'NOT_CONFIRMED'
    assert status_from_evidence(rows,cohort,tuple(rows),cleanup=False,source_ok=True) == 'NOT_CONFIRMED'
    assert status_from_evidence(rows,cohort,tuple(rows),cleanup=True,source_ok=False) == 'NOT_CONFIRMED'
    rows['ETHUSDT']['risk_status']='NOT_CONFIRMED'
    assert status_from_evidence(rows,cohort,tuple(rows),cleanup=True,source_ok=True) == 'NOT_CONFIRMED'


def _fake_runtime(monkeypatch, tmp_path, *, stale=False, cleanup_fails=False):
    import sys
    import time
    import types
    import tools.bybit_portfolio_gate as gate
    class FakeSource:
        instances=[]
        def __init__(self):
            self.closed=False
            self.symbols=[]
            self.instances.append(self)
        def set_position_symbols(self,symbols):self.symbols=list(symbols)
        def position_orderbook(self,symbol):return {'at':time.time()}
        def refresh_position_risk(self,symbol):return {
            'tiers':[{'exchange':'source'}],
            'risk_at':time.time()-400 if stale else time.time()}
        def position_candles(self,symbol,required):
            end=(int(time.time())//60)*60000
            return [{'t':end-(99-i)*60000} for i in range(100)]
        def cached_position_observations(self):
            return ({s:{'at':time.time()} for s in self.symbols},
                    {s:{'at':time.time()} for s in self.symbols})
        def close(self):self.closed=True
    class FakeRegistry:
        def __init__(self, path):self.path=path
        def subscribe(self,*args,**kwargs):return 'id'
    class FakeClock:
        evidence={'status':'PASS'}
        def ensure(self):pass
    def module(name, **attributes):
        value=types.ModuleType(name)
        for key,x in attributes.items():setattr(value,key,x)
        monkeypatch.setitem(sys.modules,name,value)
        return value
    module('bcc.hub_supervisor',ensure_hub=lambda root,bybit_ws:None)
    module('bcc.market',PublicData=FakeSource,SourceAccessDenied=type('SourceAccessDenied',(RuntimeError,),{}))
    module('bcc.market_clock',clock=FakeClock(),market_now=time.time,
           quote_is_recent=lambda row,maximum:bool(row and time.time()-row['at']<maximum),
           book_is_fresh=lambda stamp:isinstance(stamp,(int,float)) and time.time()-stamp<3)
    module('bcc.market_hub',HubRegistry=FakeRegistry)
    module('bcc.indicators',aggregate=lambda bars, minutes, now,trigger:[bars[-1]])
    module('bcc.storage',atomic_json=lambda path, value:__import__('pathlib').Path(path).write_text(
        __import__('json').dumps(value),encoding='utf-8'))
    def retire(_):
        if cleanup_fails:raise RuntimeError('Refuse unowned hub termination')
    module('tools.bybit_ws_live_acceptance',stop_owned_hub=retire)
    module('tools.verify_package',verify=lambda root:{'status':'PASS','manifest_sha256':'fake-seal'},
           assert_same_snapshot=lambda a,b:b)
    return FakeSource


def test_isolated_runner_preserves_parent_env_and_retires_own_hub(monkeypatch,tmp_path):
    import tools.bybit_portfolio_gate as gate
    import json
    source = _fake_runtime(monkeypatch,tmp_path)
    monkeypatch.setenv('BCC_PUBLIC_CACHE','SENTINEL_PARENT_CACHE')
    report=gate.run(tmp_path/'new', ['BTCUSDT','ETHUSDT'],budget_seconds=15,interval_seconds=0,rounds=3)
    assert report['status']=='PUBLIC_PORTFOLIO_DATA_PASS', report
    assert report['coherent_mark_count']==2 and report['coherent_quote_count']==2
    assert all(r['book_status']==r['risk_status']==r['candles_status']=='PASS'
               for r in report['rows'].values())
    assert report['isolated_hub_retired'] and report['renewal_retired']
    assert source.instances[-1].closed is True
    assert __import__('os').environ['BCC_PUBLIC_CACHE']=='SENTINEL_PARENT_CACHE'
    assert json.loads((tmp_path/'new'/'portfolio_report.json').read_text(encoding='utf-8'))['real_orders'] is False


def test_expired_tier_blocks_pass_and_retains_provenance(monkeypatch,tmp_path):
    import tools.bybit_portfolio_gate as gate
    _fake_runtime(monkeypatch,tmp_path,stale=True)
    report=gate.run(tmp_path/'new2', ['BTCUSDT'],budget_seconds=15,interval_seconds=0,rounds=3)
    assert report['status']=='NOT_CONFIRMED'
    assert report['rows']['BTCUSDT']['risk_status']=='NOT_CONFIRMED'
    assert report['rows']['BTCUSDT']['candles_status']=='PASS'
    assert report['isolated_hub_retired']


def test_failed_cleanup_never_reports_success(monkeypatch,tmp_path):
    import tools.bybit_portfolio_gate as gate
    _fake_runtime(monkeypatch,tmp_path,cleanup_fails=True)
    report=gate.run(tmp_path/'new3', ['BTCUSDT'],budget_seconds=15,interval_seconds=0,rounds=3)
    assert report['status']=='NOT_CONFIRMED'
    assert report['isolated_hub_retired'] is False
    assert 'cleanup_error' in report


def test_sustained_cohort_denies_missing_rounds_or_over_budget_reads():
    from tools.bybit_portfolio_gate import status_from_evidence
    syms=('BTCUSDT',)
    rows={'BTCUSDT':{'book_status':'PASS','risk_status':'PASS','candles_status':'PASS'}}
    cohort={'BTCUSDT':{'mark':True,'executable_quote':True}}
    valid=[{'full_cohort':True,'read_seconds':.04}]*3
    kw=dict(cleanup=True,source_ok=True,rounds=valid,expected_rounds=3)
    assert status_from_evidence(rows,cohort,syms,**kw)=='PUBLIC_PORTFOLIO_DATA_PASS'
    kw['rounds']=valid[:2]
    assert status_from_evidence(rows,cohort,syms,**kw)=='NOT_CONFIRMED'
    kw['rounds']=valid[:2]+[{'full_cohort':True,'read_seconds':1.501}]
    assert status_from_evidence(rows,cohort,syms,**kw)=='NOT_CONFIRMED'
    kw['rounds']=valid[:2]+[{'full_cohort':False,'read_seconds':.01}]
    assert status_from_evidence(rows,cohort,syms,**kw)=='NOT_CONFIRMED'
