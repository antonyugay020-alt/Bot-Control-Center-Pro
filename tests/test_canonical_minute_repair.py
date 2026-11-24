"""Canonical repair uses real registry/cache and exact closed REST evidence."""
import json
import time

import pytest

from bcc import market_clock
from bcc.market import PublicData
from bcc.market_hub import MarketDataHub
from bcc.hub_minute_repair import URL, HOST, params_for, identity, rest_minute


def test_proactive_repair_is_once_per_active_symbol_and_shared(tmp_path,monkeypatch):
    now_ms=60061000
    start=60000000
    monkeypatch.setattr(market_clock,"market_now",lambda:now_ms/1000)
    source=PublicData()
    hub=MarketDataHub(tmp_path/"cache.sqlite",source=source,max_workers=6)
    hub.enable_split_history()
    source.shared.central_closed_history=True
    source.shared.central_repair=True
    try:
        assert hub.acquire_leadership()
        parent=dict(category="linear",symbol="BTCUSDT",interval="1",limit=10)
        hub.registry.subscribe(URL,parent,priority=True)
        hub._schedule_minute_repairs(["BTCUSDT","ETHUSDT"])
        params=params_for("BTCUSDT",start)
        k=identity(params)
        with hub.registry._connect() as db:
            row=db.execute("SELECT subscription_token FROM hub_subscriptions WHERE k=?",(k,)).fetchone()
        assert row and hub._repair_scheduled==1
        hub._schedule_minute_repairs(["BTCUSDT"])
        with hub.registry._connect() as db:
            assert db.execute("SELECT subscription_token FROM hub_subscriptions WHERE k=?",(k,)).fetchone()==row
        data=dict(retCode=0,time=now_ms,result=dict(category="linear",symbol="BTCUSDT",
            list=[[str(start),"100","102","99","101","2","200"]]))
        key,owner,_=source.shared.begin(URL,params,HOST)
        assert key==k
        assert source.shared.finish(key,owner,HOST,data,ttl=60)
        from bcc.data_cache import PublicCache, ProviderDenied
        reader=PublicCache(source.shared.path)
        try:
            assert reader.read_closed_minutes("BTCUSDT",[start],include_rest=True)[0]["close"]==101
            assert reader.read_closed_minutes("BTCUSDT",[start])==[]
            source.shared.record_http_denial("a"*64,"fixture",HOST,cooldown=90,denial_status=403)
            with pytest.raises(ProviderDenied):
                reader.read_closed_minutes("BTCUSDT",[start],include_rest=True)
        finally:
            reader.close()
    finally:
        hub.close()


@pytest.mark.parametrize("kind",["wrong_symbol","open","wrong_minute","ws","nan","future","retcode"])
def test_canonical_repair_rejects_invalid_evidence(kind):
    start=60000000
    now=start+61000
    d=dict(retCode=0,time=now,result=dict(category="linear",symbol="BTCUSDT",
       list=[[str(start),"100","102","99","101","2","200"]]))
    if kind=="wrong_symbol":d["result"]["symbol"]="ETHUSDT"
    elif kind=="open":d["time"]=start+59999
    elif kind=="wrong_minute":d["result"]["list"][0][0]=str(start+60000)
    elif kind=="ws":d["_bcc_ws_kline"]=True
    elif kind=="nan":d["result"]["list"][0][4]="nan"
    elif kind=="future":d["time"]=now+6000
    else:d["retCode"]=10006
    assert rest_minute(d,"BTCUSDT",start,now) is None
