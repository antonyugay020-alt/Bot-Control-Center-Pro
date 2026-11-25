"""Closed-history reuse preserves genuine finality and provider fences."""
import pytest
from bcc.market import PublicData, DataUnavailable, WebSocketKlineUnavailable
from bcc import market as market_module


def test_closed_history_reuses_final_rows_and_never_promotes_forming_candle(monkeypatch):
    now=[6000.1]
    monkeypatch.setattr(market_module.clock,"ensure",lambda:None)
    monkeypatch.setattr(market_module,"market_now",lambda:now[0])
    source=PublicData()
    calls=[]
    def get(route,params):
        calls.append(dict(params))
        boundary=int(now[0]//60)*60000
        def row(t):return [str(t),"100","101","99","100","1"]
        if params["limit"]==10:
            return {"time":int(now[0]*1000),"_bcc_ws_kline":True,
                    "_bcc_ws_kline_confirmed":False,"result":{"list":[row(boundary-60000)]}}
        assert params["end"]==boundary-1
        return {"time":int(now[0]*1000),"result":{"list":[row(boundary-i*60000)
                                                           for i in range(1,101)]}}
    monkeypatch.setattr(source,"bybit",get)
    try:
        first=source.closed_candles("BTCUSDT",100)
        assert len(first)==100 and len(calls)==1
        now[0]+=10
        assert source.closed_candles("BTCUSDT",100)==first and len(calls)==1
        now[0]+=60
        # Unconfirmed WS row cannot be promoted: actual REST backfill needed.
        next_rows=source.closed_candles("BTCUSDT",100)
        assert len(calls)==3 and next_rows[-1]["t"]==first[-1]["t"]+60000
        monkeypatch.setattr(source,"_assert_provider_allowed",lambda host: (_ for _ in ()).throw(DataUnavailable("denied")))
        with pytest.raises(DataUnavailable):source.closed_candles("BTCUSDT",100)
    finally:source.close()


def test_complete_closed_window_does_not_pin_an_incomplete_or_stale_response():
    url="https://api.bybit.com/v5/market/kline"
    end=6000000-1
    params={"category":"linear","symbol":"BTCUSDT","interval":"1","limit":2,"end":end}
    rows=[[str(t),"100","101","99","100","1"] for t in (5940000,5880000)]
    good={"time":6000100,"result":{"list":rows}}
    assert PublicData._complete_closed_window(url,params,good)
    assert not PublicData._complete_closed_window(url,params,{"time":end,"result":{"list":rows}})
    assert not PublicData._complete_closed_window(url,params,{"time":6000100,"result":{"list":rows[1:]}})
    assert not PublicData._complete_closed_window(url,params,{"time":6000100,"result":{"list":[rows[0],rows[0]]}})
    assert not PublicData._complete_closed_window(url,dict(params,end=end+1),good)


def test_confirmed_ws_close_avoids_rest_and_new_boundary_rejects_old_rows(monkeypatch):
    now=[6000.1]
    monkeypatch.setattr(market_module.clock,"ensure",lambda:None)
    monkeypatch.setattr(market_module,"market_now",lambda:now[0])
    source=PublicData()
    calls=[]
    def get(route,params):
        calls.append(dict(params))
        boundary=int(now[0]//60)*60000
        def row(t):return [str(t),"100","101","99","100","1"]
        if params["limit"]==10:
            return {"time":int(now[0]*1000),"_bcc_ws_kline":True,
                    "_bcc_ws_kline_confirmed":True,"result":{"list":[row(boundary-60000)]}}
        return {"time":int(now[0]*1000),"result":{"list":[row(boundary-i*60000)
                                                           for i in range(1,101)]}}
    monkeypatch.setattr(source,"bybit",get)
    try:
        first=source.closed_candles("BTCUSDT",100)
        now[0]+=60
        second=source.closed_candles("BTCUSDT",100)
        assert len(calls)==2 and calls[-1]["limit"]==10
        assert second[-1]["t"]==first[-1]["t"]+60000
        assert len(second)==100
    finally:source.close()
