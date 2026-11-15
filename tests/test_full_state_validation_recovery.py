"""Complete validation and current health survive large-state optimization."""
from copy import deepcopy
import json
import time
import uuid

import pytest

from bcc.storage import Store,AccountingError
from bcc.market_health import record_market_outcome,display_market_status


@pytest.mark.parametrize('fault',['secret','nan','positive_inf','negative_inf','depth'])
def test_nested_mutation_is_revalidated_and_rolled_back_after_full_scan(tmp_path,fault):
    store=Store(tmp_path,500)
    with store.transaction(False) as (state,_):
        state['diagnostics']={f'C{i}USDT':[{'reason':'waiting','nested':[{'observed':1.}]} for _ in range(8)] for i in range(821)}
        state['clocks']={f'{i}:{j}':{'at':1.} for i in range(821) for j in range(8)}
    before=store.read(False)
    value={'walletPrivateKey':'not-a-real-key'} if fault=='secret' else float({'nan':'nan','positive_inf':'inf','negative_inf':'-inf'}.get(fault,'0'))
    if fault=='depth':
        value={}
        for _ in range(41):value={'ordinary':value}
    with pytest.raises(AccountingError):
        with store.transaction(False) as (state,_):state['diagnostics']['C820USDT'][7]['nested'][0]['observed']=value
    assert Store(tmp_path).read(False)==before
    store.audit();assert store.history()==[]


def test_finite_receipts_cannot_commit_an_overflowed_derived_realized_sum(tmp_path):
    store=Store(tmp_path,500)
    def append(events):events.append(dict(id=uuid.uuid4().hex,at=time.time(),symbol='TEST',section='LEGACY',action='LEGACY',cash_delta=0,net_pnl=1e308))
    with store.transaction(False) as (_,events):append(events)
    before=store.read(False);store.audit()
    with pytest.raises(AccountingError):
        with store.transaction(False) as (_,events):append(events)
    assert store.read(False)==before and len(store.history())==1;store.audit()


def test_current_health_recovers_per_symbol_channel_without_erasing_error_history():
    health={'market_errors':6,'last_completed_utc':99}
    record_market_outcome(health,'ADAUSDT',quote='book unavailable',history='candles unavailable')
    record_market_outcome(health,'BTCUSDT',quote='',history='')
    assert 'ошибки' in display_market_status('Работает',{'market_health':health},now=100)
    record_market_outcome(health,'ADAUSDT',quote='')
    record_market_outcome(health,'ADAUSDT')  # Metadata-only ticks prove neither channel.
    assert health['unavailable_symbols']=={'ADAUSDT':{'history':'candles unavailable'}}
    record_market_outcome(health,'ADAUSDT',history='')
    assert display_market_status('Работает',{'market_health':health},now=100)=='Работает'
    assert health['market_errors']==6


def test_real_worker_publishes_recovery_instead_of_a_sticky_historical_warning(tmp_path,monkeypatch):
    from bcc import runtime
    from bcc.config import defaults
    from bcc.scheduler import MarketTick
    from bcc.market import DataUnavailable
    from test_core import book
    from test_worker_observation_owner import NeverTrades
    cfg=defaults('VELES');cfg['symbols']=['BTCUSDT'];store=Store(tmp_path,500)
    class Scheduler:
        done=False
        calls=0
        def collect(self,*args):
            self.calls+=1
            if self.calls==1:return [('BTCUSDT',None,DataUnavailable('initial quote interruption'))]
            if self.calls==2:return [('BTCUSDT',MarketTick('BTCUSDT',[],book(),[],{}),None)]
            self.done=True;return []
        def close(self):pass
    scheduler=Scheduler()
    monkeypatch.setattr(runtime,'MarketScheduler',lambda *a,**k:scheduler)
    runtime.run_loop(tmp_path,cfg,store,object(),NeverTrades(),{},lambda:scheduler.done,lambda *a:100,None)
    status=json.loads((tmp_path/'process.json').read_text(encoding='utf-8'))
    assert status['market_health']['market_errors']==1
    assert status['market_health']['unavailable_symbols']=={}
    assert display_market_status('Работает',status)=='Работает'
    store.audit();assert not store.history()
