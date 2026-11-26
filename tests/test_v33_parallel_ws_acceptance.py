"""Central WS acceptance must observe both independent channels concurrently."""
from copy import deepcopy
import json
import threading
import time
from types import SimpleNamespace

import pytest

from tools import bybit_ws_live_acceptance as qa
from tools.ws_pair_delivery import collect_central_evidence, central_evidence_problems


def test_parallel_pair_starts_both_scalar_reads_before_either_can_finish(monkeypatch):
    monkeypatch.setattr(qa.clock, 'ensure', lambda: None)
    rendezvous = threading.Barrier(2, timeout=2.)
    calls = []
    class Reader:
        stop_event = threading.Event()
        def get(self, *args):
            rendezvous.wait()
            calls.append('book')
        def _ws_position_mark(self, symbol):
            rendezvous.wait()
            calls.append('mark')
    def coherent(source, symbol, row):
        assert sorted(calls) == ['book','mark']
        row['mark_price_verified'] = True
    monkeypatch.setattr(qa, '_sample_pair', coherent)
    row = qa.probe(Reader(), 'BTCUSDT', require_mark=True, parallel=True)
    assert row['status'] == 'PASS' and row['parallel_pair'] is True
    assert row['snapshot_attempts'] == 1
    assert row['stage_seconds']['parallel_pair_wait'] >= 0


def test_parallel_pair_preserves_explicit_provider_denial(monkeypatch):
    monkeypatch.setattr(qa.clock, 'ensure', lambda: None)
    class Reader:
        stop_event = threading.Event()
        def get(self,*args):
            return {}
        def _ws_position_mark(self,symbol):
            raise qa.SourceAccessDenied('api.bybit.com',451)
    monkeypatch.setattr(qa,'_sample_pair',lambda *a:pytest.fail('denied pair was sampled'))
    row=qa.probe(Reader(),'BTCUSDT',require_mark=True,parallel=True)
    assert row['status']=='FAIL' and '451' in row['error']
    assert row['failed_stage']=='independent_mark_get'


def test_parallel_pair_does_not_reset_delivery_deadline(monkeypatch):
    monkeypatch.setattr(qa.clock, 'ensure', lambda: None)
    monkeypatch.setattr(qa, 'STEADY_READ_BUDGET', .02)
    class Reader:
        stop_event=threading.Event()
        def get(self,*args):
            time.sleep(.05)
            return {}
        def _ws_position_mark(self,symbol):
            return {}
    monkeypatch.setattr(qa,'_sample_pair',lambda *a:pytest.fail('over-budget pair accepted'))
    row=qa.probe(Reader(),'BTCUSDT',require_mark=True,parallel=True)
    assert row['status']=='FAIL' and row['received_in_seconds']>.02


def test_parallel_pair_rechecks_original_source_evidence(monkeypatch):
    monkeypatch.setattr(qa.clock, 'ensure', lambda: None)
    monkeypatch.setattr(qa,'STEADY_READ_BUDGET',.04)
    class Reader:
        stop_event=threading.Event()
        def get(self,*args):return {}
        def _ws_position_mark(self,symbol):return {}
    def expired(*args):
        raise qa.DataUnavailable('stale coherent pair')
    monkeypatch.setattr(qa,'_sample_pair',expired)
    row=qa.probe(Reader(),'BTCUSDT',require_mark=True,parallel=True)
    assert row['status']=='FAIL' and row['failed_stage']=='coherent_cache_pair'
    assert 'mark_price_verified' not in row


def test_central_receipts_are_observed_not_inferred_from_flag(tmp_path):
    log=tmp_path/'market_hub.log'
    (tmp_path/'market_cache.sqlite.demand.json').write_text('secret must not enter report',encoding='utf-8')
    log.write_text('Market Hub WS health: '+json.dumps({'central_demand':None})+'\n',encoding='utf-8')
    report=dict(central_demand_requested=True,central_transport=collect_central_evidence(tmp_path),
                rounds=[[dict(parallel_pair=True)]])
    assert central_evidence_problems(report,symbols=['BTCUSDT'])
    with log.open('a',encoding='utf-8') as stream:
        stream.write('Market Hub WS health: '+json.dumps({'central_demand':{'committed':2,'batches':1}})+'\n')
    report['central_transport']=collect_central_evidence(tmp_path)
    assert not central_evidence_problems(report,symbols=['BTCUSDT'])
    assert 'secret' not in json.dumps(report)
    (tmp_path/'market_cache.sqlite.writer.lock').touch()
    report['central_transport']=collect_central_evidence(tmp_path)
    assert central_evidence_problems(report,symbols=['BTCUSDT'])


@pytest.mark.parametrize('damage',['flag','parallel','receipts','endpoint','queue'])
def test_central_evidence_gate_rejects_incomplete_or_legacy_mode(damage):
    report=dict(central_demand_requested=True, rounds=[[dict(parallel_pair=True)]],
        central_transport=dict(endpoint_observed=True,file_writer_queue_observed=False,
                               central_demand_last=dict(committed=20,batches=2)))
    if damage=='flag':report['central_demand_requested']=False
    elif damage=='parallel':report['rounds'][0][0]['parallel_pair']=False
    elif damage=='receipts':report['central_transport']['central_demand_last']['committed']=1
    elif damage=='endpoint':report['central_transport']['endpoint_observed']=False
    elif damage=='queue':report['central_transport']['file_writer_queue_observed']=True
    assert central_evidence_problems(report,symbols=qa.SYMBOLS)


@pytest.mark.parametrize('has_receipts', [True, False])
def test_live_stage_enables_central_hub_before_spawn_and_requires_receipts(tmp_path, monkeypatch, has_receipts):
    import os
    monkeypatch.delenv('BCC_PUBLIC_DEMAND_HUB_CANDIDATE', raising=False)
    monkeypatch.setenv('BCC_BYBIT_REST_MARK_FALLBACK','1')
    seal=dict(status='PASS',manifest_sha256='a'*64)
    monkeypatch.setattr(qa,'verify',lambda root:seal)
    monkeypatch.setattr(qa,'assert_same_snapshot',lambda a,b:b)
    monkeypatch.setattr(qa,'clock',SimpleNamespace(ensure=lambda:None,evidence={}))
    monkeypatch.setattr(qa.time,'sleep',lambda seconds:None)
    monkeypatch.setattr(qa,'stop_owned_hub',lambda root:None)
    monkeypatch.setattr(qa,'PublicData',lambda:SimpleNamespace(close=lambda:None))
    def spawn(root, **kwargs):
        assert os.environ['BCC_PUBLIC_DEMAND_HUB_CANDIDATE']=='1'
        assert 'BCC_BYBIT_REST_MARK_FALLBACK' not in os.environ
        (root/'market_cache.sqlite.demand.json').touch()
        stats=dict(committed=20,batches=1) if has_receipts else None
        (root/'market_hub.log').write_text('Market Hub WS health: '+json.dumps({'central_demand':stats})+'\n',encoding='utf-8')
    monkeypatch.setattr(qa,'ensure_hub',spawn)
    def probe(source,symbol,**kwargs):
        assert kwargs['parallel'] is True and kwargs['require_mark'] is True
        return dict(symbol=symbol,status='PASS',parallel_pair=True,mark_price_verified=True,
                    mark_age_lower_seconds=.1,mark_age_seconds=.2,book_age_lower_seconds=.1,
                    age_upper_seconds=.2,received_in_seconds=.1)
    monkeypatch.setattr(qa,'probe',probe)
    out=tmp_path/'out'
    code=qa.main(['--output',str(out),'--require-mark','--central-demand'])
    result=json.loads((out/'live_ws_report.json').read_text(encoding='utf-8'))
    assert code==(0 if has_receipts else 1)
    assert result['status']==('PUBLIC_WS_HUB_CLIENT_PASS' if has_receipts else 'BLOCKED_INCOMPLETE_EVIDENCE')
    assert 'BCC_PUBLIC_DEMAND_HUB_CANDIDATE' not in os.environ
    assert os.environ['BCC_BYBIT_REST_MARK_FALLBACK']=='1'


from test_bybit_coherent_acceptance import sample, portfolio


@pytest.mark.parametrize('damaged', [False, True])
def test_parallel_transport_still_requires_real_coherent_cache_snapshot(sample, monkeypatch, damaged):
    symbol, reader, _, _ = sample
    original = reader.shared.read_fresh_many
    if damaged:
        monkeypatch.setattr(qa, 'STEADY_READ_BUDGET', .5)
        def read(*args, **kwargs):
            book, ticker = original(*args, **kwargs)
            ticker['_bcc_ws_mark'] = False
            return [book,ticker]
        monkeypatch.setattr(reader.shared,'read_fresh_many',read)
    row=qa.probe(reader,symbol,require_mark=True,parallel=True)
    assert row['parallel_pair'] is True
    if damaged:
        assert row['status']=='FAIL' and row['failed_stage']=='coherent_cache_pair',row
        assert 'mark_price_verified' not in row
    else:
        assert row['status']=='PASS' and row['mark_price_verified'] is True,row
        assert row['age_upper_seconds']<=3 and row['mark_age_seconds']<=3


def test_native_verifier_propagates_central_requirement_only_to_ws_stage(tmp_path, monkeypatch):
    import sys
    from tools import verify_windows, verify_package
    out=tmp_path/'out'
    monkeypatch.setattr(sys,'argv',['verify_windows','--output',str(out),'--venue','bybit','--central-demand'])
    monkeypatch.setattr(verify_windows,'is_native_windows',lambda:True)
    monkeypatch.setattr(verify_windows,'source_fingerprint',lambda:'b'*64)
    seal=dict(status='PASS',manifest_sha256='a'*64)
    monkeypatch.setattr(verify_package,'verify',lambda root:seal)
    monkeypatch.setattr(verify_package,'assert_same_snapshot',lambda a,b:b)
    stages=[]
    def step(name, arguments, output, timeout):
        stages.append(name)
        assert ('--central-demand' in arguments)==(name=='bybit_ws')
        return dict(step=name,status='FAIL' if name=='bybit_ws' else 'PASS')
    monkeypatch.setattr(verify_windows,'run_step',step)
    def evidence(output, *, venue, central_demand):
        assert venue=='bybit' and central_demand is True
        return ['simulated missing central receipt']
    monkeypatch.setattr(verify_windows,'validate_required_evidence',evidence)
    assert verify_windows.main()==1
    assert 'bybit_ws' in stages and 'public_sources' not in stages
