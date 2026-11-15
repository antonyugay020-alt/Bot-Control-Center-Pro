"""Repeated risk diagnostics are compact on disk and exact after restart."""
import json
from copy import deepcopy

import pytest

from bcc.storage import Store, AccountingError
from bcc.config import is_secret_key


def test_large_repeated_diagnostics_roundtrip_and_accounting_are_exact(tmp_path):
    store=Store(tmp_path,500)
    reason='Устарели публичные ступени риска '+'; '.join(f'COIN{i}USDT' for i in range(20))
    diagnostics={f'COIN{i}USDT':[{'section':f'RSI {n}','reason':reason,'conditions':[{'value':30.}]} for n in range(8)] for i in range(821)}
    expected=deepcopy(diagnostics)
    with store.transaction(False) as (state,_):state['diagnostics']=diagnostics
    assert diagnostics==expected
    reopened=Store(tmp_path);assert reopened.read(False)['diagnostics']==expected;reopened.audit()
    with store.connect() as db:raw=db.execute('SELECT document FROM state').fetchone()[0]
    packed=json.loads(raw);assert packed['_diagnostics_v1']['reasons']==[reason]
    assert len(raw)<len(json.dumps(expected,ensure_ascii=False))/2
    with reopened.transaction(False) as (state,_):state['diagnostics']['COIN0USDT'][0]['reason']='recovered'
    assert reopened.read(False)['diagnostics']['COIN1USDT'][0]['reason']==reason
    assert reopened.history()==[];reopened.audit()


@pytest.mark.parametrize('damage',['missing_reason','bad_index','bool_index','dict_reason','duplicate_field','bad_row','none'])
def test_corrupt_diagnostic_encoding_blocks_reads_and_financial_transactions(tmp_path,damage):
    store=Store(tmp_path,500)
    with store.transaction(False) as (state,_):
        state['diagnostics']={'BTCUSDT':[{'reason':'same reason '*20} for _ in range(3)]}
    with store.connect() as db:
        data=json.loads(db.execute('SELECT document FROM state').fetchone()[0]);codec=data['_diagnostics_v1']
        if damage=='missing_reason':codec['reasons']=[]
        if damage=='bad_index':codec['rows']['BTCUSDT'][0][0]=123
        if damage=='bool_index':codec['rows']['BTCUSDT'][0][0]=True
        if damage=='dict_reason':codec['reasons'][0]={'secret_key':'forbidden'}
        if damage=='duplicate_field':data['diagnostics']={}
        if damage=='bad_row':codec['rows']['BTCUSDT'][0]='bad'
        if damage=='none':data['_diagnostics_v1']=None
        db.execute('UPDATE state SET document=?',(json.dumps(data),))
    with pytest.raises(AccountingError):store.read(False)
    with pytest.raises(AccountingError):
        with store.transaction(False):pass


def test_key_classification_cache_keeps_namespaced_secret_and_legacy_input_rules():
    for _ in range(100):
        assert is_secret_key('polymarket_relayer_api_key')
        assert is_secret_key('walletPrivateKey')
        assert not is_secret_key('token') and not is_secret_key(['ordinary','key'])
    assert is_secret_key('wallet-private-key')
