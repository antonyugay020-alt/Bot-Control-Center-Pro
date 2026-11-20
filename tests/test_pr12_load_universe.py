"""The isolated load scenario must follow the configured symbol universe."""
import json
import time

import pytest

from tools.soak_pr12_scheduled_hub import capture_probe_inputs, load_universe


def test_load_scenario_accepts_828_symbols_and_17_held(tmp_path):
    symbols=[f'ASSET{i}USDT' for i in range(828)]
    path=tmp_path/'symbols.json'
    path.write_text(json.dumps(symbols),encoding='utf-8')
    loaded,held=load_universe(path,17)
    assert loaded==symbols and held==symbols[:17]


def test_load_scenario_rejects_duplicate_symbols_and_insufficient_fault_space(tmp_path):
    path=tmp_path/'symbols.json'
    path.write_text(json.dumps(['ASSETUSDT']*30),encoding='utf-8')
    with pytest.raises(ValueError):load_universe(path,17)
    path.write_text(json.dumps([f'ASSET{i}USDT' for i in range(26)]),encoding='utf-8')
    with pytest.raises(ValueError):load_universe(path,17)


def test_probe_rebases_real_book_after_slow_history_and_portfolio():
    base=time.time();clock=[base-5];original=[]
    class Reader:
        def candles(self,symbol,count):
            clock[0]+=4  # A previously obtained three-second book would expire.
            return [{'time':clock[0]}]
        def orderbook(self,symbol):
            depth={'at':clock[0],'evidence':'original book'};original.append(depth)
            return depth
        def cached_position_observations(self):
            clock[0]+=1
            quote={'bid':100,'ask':100,'at':clock[0]}
            return {'HELD':quote},{'HELD':quote}
        def cached_execution_book(self,symbol,depth):
            assert depth is original[0]
            return {'at':clock[0],'evidence':'independent fresh book'}
    bars,depth,observations=capture_probe_inputs(Reader(),'TARGET',['HELD'])
    assert bars[0]['time']==base-1 and observations[0]['HELD']['at']==base
    assert original[0]['at']==base-1
    assert depth=={'at':base,'evidence':'independent fresh book'}
