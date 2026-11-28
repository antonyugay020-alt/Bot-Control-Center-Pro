"""Strict native WS profile isolation and observed transport proof."""
from copy import deepcopy
import json
import zlib

import pytest

from tools.ws_native_profile import FLAGS, PREFIX, profile_evidence, step_environment

SYMBOLS = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'DOGEUSDT',
           'ADAUSDT', 'LINKUSDT', 'AVAXUSDT', 'AUSDT', '4USDT')


def health():
    counts = [sum(zlib.crc32(s.encode('ascii')) % 4 == i for s in SYMBOLS) for i in range(4)]
    def lane(n):
        return dict(connection='connected', subscribed=n)
    stream = dict(mode='persistent_sharded_redundancy', shard_count=4,
                  mark_union=dict(active=len(SYMBOLS)), maintained={}, maintained_recovery={},
                  primary=lane(len(SYMBOLS)), recovery=lane(len(SYMBOLS)),
                  shards=[dict(index=i, union=dict(active=n), primary=lane(n), recovery=lane(n))
                          for i,n in enumerate(counts)])
    return dict(mark_delivery=dict(enabled=True, balanced_batch=True, sharded=True,
                                   rest_fallback=None, hybrid_cache=None),
                streams=dict(bybit_quotes=stream))


def log(tmp_path, rows):
    path = tmp_path / 'market_hub.log'
    path.write_text(''.join(PREFIX+json.dumps(r)+'\n' for r in rows), encoding='utf-8')
    return path


@pytest.mark.parametrize('name,args', [('tests', ['--central-demand']), ('gui', []),
                                      ('public_sources', []), ('bybit_ws', [])])
def test_other_stages_unchanged(name, args):
    env = {'PATH':'fixture', 'BCC_BYBIT_REST_MARK_FALLBACK':'1'}
    assert step_environment(env, name, args) == env


def test_native_profile_isolated_and_rest_disabled():
    env = dict.fromkeys(FLAGS, '0')
    env['BCC_BYBIT_REST_MARK_FALLBACK'] = '1'
    env['PATH'] = 'fixture'
    result = step_environment(env, 'bybit_ws', ['--central-demand'])
    assert all(result[k]=='1' and env[k]=='0' for k in FLAGS)
    assert 'BCC_BYBIT_REST_MARK_FALLBACK' not in result
    assert env['BCC_BYBIT_REST_MARK_FALLBACK']=='1' and result['PATH']=='fixture'


def test_observed_complete_profile(tmp_path):
    row = health()
    early = deepcopy(row)
    early['streams']['bybit_quotes']['mark_union']['active'] = 0
    report = profile_evidence(log(tmp_path,[early,row]), SYMBOLS)
    assert report['status']=='PASS' and report['samples']==2 and report['proven_samples']==1
    assert sum(report['expected_shard_counts'])==10


@pytest.mark.parametrize('damage', ['legacy','rest','hybrid','missing_rest','missing_hybrid',
    'missing_shard','duplicate_shard','wrong_inventory','missing_reserve','wrong_primary',
    'missing_maintained','reconnecting','missing_health'])
def test_profile_rejects_incomplete_evidence(tmp_path,damage):
    row=health()
    d=row['mark_delivery']; q=row['streams']['bybit_quotes']; shards=q['shards']
    if damage=='legacy': d['sharded']=False
    elif damage=='rest': d['rest_fallback']={}
    elif damage=='hybrid': d['hybrid_cache']={}
    elif damage=='missing_rest': del d['rest_fallback']
    elif damage=='missing_hybrid': del d['hybrid_cache']
    elif damage=='missing_shard': shards.pop()
    elif damage=='duplicate_shard': shards[1]['index']=0
    elif damage=='wrong_inventory': shards[0]['union']['active']+=1
    elif damage=='missing_reserve': del shards[0]['recovery']
    elif damage=='wrong_primary': q['primary']['subscribed']-=1
    elif damage=='missing_maintained': del q['maintained_recovery']
    elif damage=='reconnecting': shards[0]['primary']['connection']='reconnecting'
    elif damage=='missing_health': row={}
    proof=profile_evidence(log(tmp_path,[row]),SYMBOLS)
    assert proof['status']=='FAIL' and proof['errors']


def test_rest_at_any_sample_is_not_masked_by_later_pass(tmp_path):
    row=health(); earlier=deepcopy(row); earlier['mark_delivery']['rest_fallback']={'completed':1}
    proof=profile_evidence(log(tmp_path,[earlier,row]),SYMBOLS)
    assert proof['proven_samples']==1 and proof['status']=='FAIL'


def test_missing_or_corrupt_log_cannot_pass(tmp_path):
    path=tmp_path/'missing.log'
    with pytest.raises(OSError): profile_evidence(path,SYMBOLS)
    path.write_text(PREFIX+'{broken\n',encoding='utf-8')
    with pytest.raises(ValueError): profile_evidence(path,SYMBOLS)


def test_exact_native_runner_passes_profile_only_to_ws(tmp_path, monkeypatch):
    from tools import verify_windows
    calls = []
    def execute(command, **kwargs):
        calls.append((command, kwargs['env']))
        return 0
    monkeypatch.setattr(verify_windows, 'run_logged_command', execute)
    monkeypatch.setenv('BCC_BYBIT_REST_MARK_FALLBACK', '1')
    for key in FLAGS:
        monkeypatch.delenv(key, raising=False)
    assert verify_windows.run_step('bybit_ws', ['--central-demand'], tmp_path, 10)['status']=='PASS'
    assert verify_windows.run_step('gui', [], tmp_path, 10)['status']=='PASS'
    assert all(calls[0][1][key]=='1' for key in FLAGS)
    assert 'BCC_BYBIT_REST_MARK_FALLBACK' not in calls[0][1]
    assert all(key not in calls[1][1] for key in FLAGS)
    assert calls[1][1]['BCC_BYBIT_REST_MARK_FALLBACK']=='1'


@pytest.mark.parametrize('damaged', [False, True])
def test_native_required_evidence_replays_profile_log(tmp_path, monkeypatch, damaged):
    from tools import verify_windows, bybit_ws_live_acceptance, verify_package
    ws=tmp_path/'bybit_ws'; hub=ws/'isolated_hub'; hub.mkdir(parents=True)
    (ws/'live_ws_report.json').write_text('{}', encoding='utf-8')
    monkeypatch.setattr(bybit_ws_live_acceptance, 'validate_report', lambda *a, **kw: [])
    monkeypatch.setattr(verify_package, 'verify', lambda *a: {'manifest_sha256':'fixture'})
    row=health()
    if damaged:
        row['mark_delivery']['sharded']=False
    log(hub,[row])
    errors=verify_windows.validate_required_evidence(tmp_path,venue='bybit',central_demand=True)
    proof=json.loads((ws/'native_ws_profile.json').read_text(encoding='utf-8'))
    assert proof['status']==('FAIL' if damaged else 'PASS')
    assert any('WS-only native profile:' in e for e in errors)==damaged
