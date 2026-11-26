"""Exercise lease reuse and admission policy against real SQLite WAL."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import sqlite3
import time

import pytest

from bcc.hub_demand_policy import commit_demand_batch
from bcc.writer_priority import PriorityWaiters


@pytest.fixture
def registry(tmp_path):
    path = tmp_path/'public.sqlite'
    @contextmanager
    def connect():
        db = sqlite3.connect(path, timeout=2., isolation_level=None)
        try:
            with db:
                yield db
        finally:
            db.close()
    with connect() as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE hub_subscriptions(k TEXT PRIMARY KEY,url TEXT,params TEXT,active_until REAL,priority_until REAL,next_due REAL,subscription_token TEXT)')
        db.execute('CREATE TABLE leader(owner TEXT)')
        db.execute("INSERT INTO leader VALUES('owner')")
        db.execute('CREATE TABLE market(k INTEGER PRIMARY KEY,v INTEGER)')
    @contextmanager
    def write_connect():
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            yield db
    def leader_check(db, now):
        if db.execute('SELECT owner FROM leader').fetchone() != ('owner',):
            raise RuntimeError('lost generation')
    def write(db, row, now):
        key,url,params,lifetime,priority,force_due = row
        db.execute('INSERT OR REPLACE INTO hub_subscriptions VALUES(?,?,?,?,?,?,?)',
                   (key,url,params,now+lifetime,now+min(lifetime*.75,90) if priority else 0,
                    0,'token'))
    def demand(rows, writer=write_connect):
        return commit_demand_batch(rows,connect=connect,write_connect=writer,
                                   leader_check=leader_check,write=write)
    return connect, write_connect, demand


def row(i=0, *, priority=True, force_due=False, lifetime=12):
    return (str(i),'public',str(i),lifetime,priority,force_due)


def test_duplicate_and_covered_requests_do_not_write(registry):
    connect, _, demand = registry
    first = demand([row(),row(),row()])
    assert first == ['0']*3 and first.written_rows == first.write_batches == 1
    def forbidden():
        pytest.fail('covered request attempted a write transaction')
    repeat = demand([row()]*32, forbidden)
    assert repeat == ['0']*32 and repeat.written_rows == repeat.write_batches == 0


@pytest.mark.parametrize('damage', ['lease','priority','identity','token','parked'])
def test_insufficient_coverage_reaches_writer(registry, damage):
    connect, _, demand = registry
    request = row(force_due=damage=='parked')
    demand([request])
    with connect() as db:
        mutations = {'lease':('active_until',time.time()+1),
                     'priority':('priority_until',time.time()+1),
                     'identity':('params','other'), 'token':('subscription_token',''),
                     'parked':('next_due',time.time()+3600)}
        column,value = mutations[damage]
        db.execute('UPDATE hub_subscriptions SET '+column+'=?',(value,))
    receipt = demand([request])
    assert receipt.written_rows == receipt.write_batches == 1


def test_generation_rechecked_before_mutating(registry):
    connect, write_connect, demand = registry
    @contextmanager
    def changed_generation():
        with connect() as db:
            db.execute("UPDATE leader SET owner='successor'")
        with write_connect() as db:
            yield db
    with pytest.raises(RuntimeError, match='lost generation'):
        demand([row()],changed_generation)
    with connect() as db:
        assert db.execute('SELECT count(*) FROM hub_subscriptions').fetchone()[0]==0


def test_generation_loss_rejects_existing_coverage(registry):
    connect, _, demand = registry
    demand([row()])
    with connect() as db:
        db.execute("UPDATE leader SET owner='successor'")
    with pytest.raises(RuntimeError,match='lost generation'):
        demand([row()])


def test_59_symbols_concurrent_receipts_do_not_contend_with_publication(registry):
    connect, write_connect, demand = registry
    # 59 real subscription records; three clients repeatedly request the same
    # admitted demand while 240 market transactions exercise the WAL writer.
    for i in range(0,59,32):
        demand([row(j,lifetime=90,force_due=True) for j in range(i,min(i+32,59))])
    def reader(worker):
        count=0
        for i in range(400):
            receipt=demand([row((i+worker)%59,lifetime=90,force_due=True)])
            assert receipt.write_batches==receipt.written_rows==0
            count+=len(receipt)
        return count
    def publish():
        for i in range(240):
            with write_connect() as db:
                db.executemany('INSERT OR REPLACE INTO market VALUES(?,?)',[(j,i) for j in range(59)])
        return 240*59
    with ThreadPoolExecutor(max_workers=4) as pool:
        clients=[pool.submit(reader,i) for i in range(3)]
        market=pool.submit(publish)
        assert sum(p.result(timeout=40) for p in clients)==1200
        assert market.result(timeout=40)==14160
    with connect() as db:
        assert db.execute('SELECT count(*) FROM market WHERE v=239').fetchone()[0]==59
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'


def test_priority_burst_is_bounded_and_fifo_per_class():
    q=PriorityWaiters()
    q.append('normal-1');q.append('normal-2')
    for i in range(6):q.append(i,True)
    grants=[]
    while len(q):
        ticket=q.head();urgent=type(ticket) is int
        q.remove(ticket,urgent);q.admitted(urgent);grants.append(ticket)
    assert grants==[0,1,2,3,'normal-1',4,5,'normal-2']


def test_expired_waiter_removal_does_not_consume_priority_grant():
    q=PriorityWaiters()
    q.append('ordinary');q.append('aborted',True);q.append('mark',True)
    q.remove('aborted',True)
    assert q.head()=='mark' and q.streak==0
    q.remove('mark',True);q.admitted(True)
    assert q.head()=='ordinary'
