"""A held local writer cannot revoke already committed public WS evidence."""
import sqlite3
import threading
import time

import pytest

from bcc.data_cache import PublicCache,CacheBusy,ProviderDenied

HOST='api.bybit.com'
URL='https://api.bybit.com/v5/market/tickers'
PARAMS={'category':'linear','symbol':'BTCUSDT'}


def seeded(tmp_path):
    cache=PublicCache(tmp_path/'shared cache #1.sqlite')
    key,owner,_=cache.begin(URL,PARAMS,HOST)
    assert cache.finish(key,owner,HOST,{'retCode':0,'price':100},ttl=60)
    return cache,key


def test_committed_reads_and_policy_checks_bypass_a_blocked_local_writer(tmp_path):
    cache,key=seeded(tmp_path);entered=threading.Event();release=threading.Event();errors=[]
    def writer():
        try:
            with cache.db() as db:
                db.execute('BEGIN IMMEDIATE');db.execute('UPDATE cache SET value=? WHERE k=?',('{"retCode":0,"price":200}',key))
                entered.set();assert release.wait(3)
                db.rollback()
        except BaseException as exc:errors.append(exc)
    thread=threading.Thread(target=writer);thread.start()
    try:
        assert entered.wait(2)
        assert cache.read_fresh(URL,PARAMS,HOST)['price']==100
        assert cache.read_fresh_many([(URL,PARAMS)],HOST)[0]['price']==100
        assert cache.denied_status(HOST) is None
        assert cache.remaining_ttl(URL,PARAMS,HOST)>0
        assert cache.stats['read_connections']==1 and cache.stats['local_busy']==0
    finally:
        release.set();thread.join(3);cache.close()
    assert not errors and not thread.is_alive()


def test_read_only_lane_preserves_committed_denial_and_actual_expiry(tmp_path):
    cache,key=seeded(tmp_path)
    try:
        with cache.db() as db:
            db.execute('BEGIN IMMEDIATE');cache._store_denial(db,HOST,60,451)
            assert cache.denied_status(HOST) is None  # Uncommitted writer is isolated.
            db.commit()
        assert cache.denied_status(HOST)==451
        for reader in (lambda:cache.read_fresh(URL,PARAMS,HOST),lambda:cache.read_fresh_many([(URL,PARAMS)],HOST),lambda:cache.remaining_ttl(URL,PARAMS,HOST)):
            with pytest.raises(ProviderDenied):reader()
        with cache.db() as db:
            db.execute('DELETE FROM provider_denial')
        key,owner,_=cache.begin(URL,PARAMS,HOST)
        assert cache.finish(key,owner,HOST,{'retCode':0,'price':100},ttl=60)
        with cache.db() as db:db.execute('UPDATE cache SET until=0')
        assert cache.read_fresh(URL,PARAMS,HOST) is None
        assert cache.read_fresh_many([(URL,PARAMS)],HOST)==[None]
        assert cache.remaining_ttl(URL,PARAMS,HOST)==0
    finally:cache.close()


def test_reader_cannot_write_and_closes_all_native_handles(tmp_path):
    cache,_=seeded(tmp_path)
    cache.read_fresh(URL,PARAMS,HOST)
    connection=cache._reader_connection
    with pytest.raises(CacheBusy):
        with cache.read_db() as db:db.execute('DELETE FROM cache')
    assert cache.read_fresh(URL,PARAMS,HOST)['price']==100
    cache.close()
    assert cache._connection is cache._reader_connection is None
    with pytest.raises(sqlite3.ProgrammingError):connection.execute('SELECT 1')
    renamed=cache.path.with_name('moved.sqlite');cache.path.rename(renamed);renamed.unlink()
