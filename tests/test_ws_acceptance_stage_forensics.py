"""Failed live WS samples retain only safe stage/provenance diagnostics."""
from types import SimpleNamespace
import time
from tools import bybit_ws_live_acceptance as acceptance
from bcc.market import DataUnavailable


def test_initial_book_timeout_is_distinct_from_mark_timeout(monkeypatch):
    monkeypatch.setattr(acceptance.clock, 'ensure', lambda: None)
    class Reader:
        stop_event=SimpleNamespace(is_set=lambda:False)
        def get(self, *args):
            raise DataUnavailable('book unavailable')
    row=acceptance.probe(Reader(), '4USDT', require_mark=True)
    assert row['status']=='FAIL'
    assert row['failed_stage']=='initial_book_get'
    assert 'book unavailable' in row['error']


def test_mark_timeout_has_explicit_stage_without_any_synthetic_mark(monkeypatch):
    monkeypatch.setattr(acceptance.clock, 'ensure', lambda: None)
    monkeypatch.setattr(acceptance, 'STEADY_READ_BUDGET', .015)
    class Reader:
        stop_event=SimpleNamespace(is_set=lambda:False)
        def get(self, *args):
            return {'_bcc_ws_book': True}
        def _ws_position_mark(self, symbol):
            raise DataUnavailable('mark absent')
    row=acceptance.probe(Reader(), 'DOGEUSDT', require_mark=True)
    assert row['status']=='FAIL'
    assert row['failed_stage']=='independent_mark_get'
    assert 'mark absent' in row['error']
    assert 'mark absent' in row['last_mark_failure']


def test_coherent_pair_failure_reports_only_origin_flags(monkeypatch):
    monkeypatch.setattr(acceptance.clock,'ensure',lambda:None)
    monkeypatch.setattr(acceptance,'STEADY_READ_BUDGET',.015)
    class Cache:
        def read_fresh_many(self, *args):
            return ({'_bcc_ws_book':True,'result':{'s':'DOGEUSDT','ts':int(time.time()*1000),'b':[['100','1']],'a':[['101','1']]}}, {'_bcc_ws_quote':True,'_bcc_ws_mark':False})
    class Reader:
        shared=Cache()
        stop_event=SimpleNamespace(is_set=lambda:False)
        def get(self,*args):
            return {'_bcc_ws_book':True}
        def _ws_position_mark(self,symbol):
            return {'at':0}
        def _assert_provider_allowed(self,*args):
            pass
    row=acceptance.probe(Reader(),'DOGEUSDT',require_mark=True)
    assert row['failed_stage']=='coherent_cache_pair'
    assert row['snapshot_flags']=={'book_cached':True,'ticker_cached':True,
                                   'mark_provenance':False,'quote_provenance':True,
                                   'generation_present':False}
    assert 'No verified Bybit WS mark provenance' in row['error']
