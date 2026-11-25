import json
import sqlite3
from types import SimpleNamespace
import pytest
from bcc.hub_minute_repair import URL, HOST, REPAIR_SQL, is_repair, params_for, identity, schedule
from bcc.hub_rest_lanes import select_history_admissions

@pytest.mark.parametrize('changes', [{}, {'limit':True}, {'limit':1.0}, {'extra':'x'}, {'end':60000001}, {'start':0}, {'interval':'5'}, {'symbol':None}])
def test_sql_shape_matches_python(changes):
    p=params_for('BTCUSDT',60000000);p.update(changes)
    with sqlite3.connect(':memory:') as db:
        got=db.execute('SELECT coalesce(('+REPAIR_SQL+'),0) FROM (SELECT ? AS params)',(json.dumps(p),)).fetchone()[0]
        assert bool(got)==is_repair(URL,p)

def test_expired_repair_rearmed_only_without_http_owner():
    db=sqlite3.connect(':memory:')
    db.executescript('''CREATE TABLE hub_leader(singleton INTEGER,owner TEXT,lease_token TEXT,expires REAL,boot_epoch INTEGER); CREATE TABLE hub_subscriptions(k TEXT PRIMARY KEY,url TEXT,params TEXT,active_until REAL,next_due REAL,priority_until REAL,subscription_token TEXT); CREATE TABLE lease(k TEXT PRIMARY KEY,until REAL); CREATE TABLE cache(k TEXT PRIMARY KEY,value TEXT,host TEXT,until REAL);''')
    db.execute('INSERT INTO hub_leader VALUES(1,?,?,?,0)',('a','token',120))
    p=dict(category='linear',symbol='BTCUSDT',interval='1',limit=10)
    db.execute('INSERT INTO hub_subscriptions VALUES(?,?,?,?,?,?,?)',(identity(p),URL,json.dumps(p,sort_keys=True,separators=(',',':')),90,0,90,'parent'))
    k=identity(params_for('BTCUSDT',60000000));p=params_for('BTCUSDT',60000000)
    db.execute('INSERT INTO hub_subscriptions VALUES(?,?,?,?,?,?,?)',(k,URL,json.dumps(p,sort_keys=True,separators=(',',':')),1,0,0,'expired'))
    db.execute('INSERT INTO lease VALUES(?,?)',(k,30))
    def run():return schedule(db,['BTCUSDT'],60000000,10,60062000,owner='a',token='token',boot=0)
    assert run()==[]
    db.execute('DELETE FROM lease')
    assert run()==['BTCUSDT']
    assert db.execute('SELECT subscription_token FROM hub_subscriptions WHERE k=?',(k,)).fetchone()[0]!='expired'
    assert run()==[]

def test_slot_selection_prefers_repairs_but_reserves_cold_history():
    repair=[(str(i),URL,params_for(f'R{i}USDT',60000000),True,'r') for i in range(3)]
    ordinary=[('hot',URL,dict(category='linear',symbol='HOTUSDT',interval='1',limit=100),True,'h'),('cold',URL,dict(category='linear',symbol='COLDUSDT',interval='1',limit=100),False,'c')]
    class Registry:
        def due(self, **kw):return list(repair if kw['repair_lane'] else ordinary)
    hub=SimpleNamespace(registry=Registry(),_inflight={},_websocket_queue_exclusions=lambda:())
    assert [r[0] for r in select_history_admissions(hub,[],3)]==['cold','0','1']
    running=[(None,URL,False,None,None,ordinary[1][2])]
    assert [r[0] for r in select_history_admissions(hub,running,2)]==['0','1']
