"""A busy portfolio crosses one fenced WAL publication boundary per cohort."""
import time

import pytest

from bcc import market_clock
from bcc.data_cache import CacheBusy, ProviderDenied, PublicCache
from bcc.market import PublicData
from bcc.market_hub import MarketDataHub
from bcc.ws_dispatch import MarketEventQueue
from bcc.ws_orderbooks import BybitOrderbookWS
from bcc.ws_quotes import BybitQuoteStream

BASE = 'https://api.bybit.com/v5/market/'
HOST = 'api.bybit.com'


@pytest.fixture
def cohort(tmp_path, monkeypatch):
    clock = market_clock.MarketClock()
    monkeypatch.setattr(market_clock, 'clock', clock)
    symbols = [f'HELD{i}USDT' for i in range(28)]
    queue = MarketEventQueue()
    quotes = BybitQuoteStream(on_update=queue.notify)
    books = BybitOrderbookWS(on_update=queue.notify)
    source = PublicData(quote_stream=quotes, orderbook_stream=books)
    hub = MarketDataHub(tmp_path/'cache.sqlite', source=source)
    reader = PublicCache(tmp_path/'cache.sqlite')
    requests = []
    quotes.subscribe_many(symbols)
    books.subscribe(symbols)
    qgen, bgen = quotes.connect(), books.connect()
    for symbol in symbols:
        for route, params in (
            ('tickers', dict(category='linear', symbol=symbol)),
            ('orderbook', dict(category='linear', symbol=symbol, limit=50)),
        ):
            requests.append((BASE + route, params))
            hub.registry.subscribe(BASE + route, params, priority=True)
    queue.configure(*hub.registry.stream_demand())
    assert hub.acquire_leadership()
    hub.source.shared._ensure_schema()
    reader._ensure_schema()
    stamp = int(time.time()*1000)
    for symbol in symbols:
        # Independent times and prices must survive batching unchanged.
        assert quotes.ingest(dict(topic='tickers.'+symbol, type='snapshot', ts=stamp-250,
            data=dict(symbol=symbol, markPrice='99.5')), generation=qgen)
        assert books.ingest(dict(topic='orderbook.50.'+symbol, type='snapshot', ts=stamp,
            data=dict(s=symbol, u=1, seq=1, b=[['100','5']], a=[['101','5']])), generation=bgen)
    try:
        yield hub, reader, queue, requests, stamp
    finally:
        reader.close()
        hub.close()


def test_28_held_symbols_publish_atomically_with_one_commit_and_independent_clocks(cohort):
    hub, reader, queue, requests, stamp = cohort
    statements, before_commit = [], []
    def trace(sql):
        statements.append(sql)
        if sql == 'COMMIT':
            before_commit.append(reader.read_fresh_many(requests, HOST))
    with hub.source.shared.db() as db:
        db.set_trace_callback(trace)
    batch = queue.take_batch(timeout=0)
    assert len(batch) == 56
    assert hub.publish_ws_events(batch) == [True]*56
    assert statements.count('BEGIN IMMEDIATE') == statements.count('COMMIT') == 1
    assert before_commit == [[None]*56], 'A reader must not see a partial portfolio'
    rows = reader.read_fresh_many(requests, HOST)
    for index, (url, params) in enumerate(requests):
        row = rows[index]
        if url.endswith('/tickers'):
            mark = PublicData._decode_ws_position_mark(params['symbol'], row)
            assert mark['at'] == (stamp-250)/1000 and mark['bid'] == 99.5
        else:
            assert row['result']['ts'] == stamp
    with hub.source.shared.db() as db:
        db.set_trace_callback(None)
        assert db.execute('SELECT COUNT(*) FROM request_budget').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM lease').fetchone()[0] == 0


@pytest.mark.parametrize('fence', ['leader', 'subscription', 'denial'])
def test_batch_keeps_durable_admission_fences(cohort, fence):
    hub, reader, queue, requests, _ = cohort
    if fence == 'leader':
        with hub.registry._connect() as db:
            db.execute("UPDATE hub_leader SET lease_token='successor'")
    elif fence == 'subscription':
        key = hub.registry.identity(*requests[0])[0]
        with hub.registry._connect() as db:
            db.execute('UPDATE hub_subscriptions SET active_until=0 WHERE k=?', (key,))
    else:
        hub.source.shared.record_http_denial('0'*64, 'denied-owner', HOST,
                                             cooldown=60, denial_status=451)
    outcomes = hub.publish_ws_events(queue.take_batch(timeout=0))
    if fence == 'subscription':
        assert outcomes.count(False) == 1 and outcomes.count(True) == 55
        rows = reader.read_fresh_many(requests, HOST)
        assert rows[0] is None and all(row is not None for row in rows[1:])
    else:
        assert not any(outcomes)
        if fence == 'denial':
            with pytest.raises(ProviderDenied):
                reader.read_fresh_many(requests, HOST)
        else:
            assert reader.read_fresh_many(requests, HOST) == [None]*56


def test_one_bad_payload_or_sql_failure_cannot_partially_publish_a_batch(cohort):
    hub, reader, queue, requests, _ = cohort
    events = queue.take_batch(timeout=0)
    records = [hub._prepare_ws_event(*event) for event in events]
    records[-1] = dict(records[-1], value={})
    with pytest.raises(ValueError):
        hub.source.shared.publish_websockets(records, leader_owner=hub.owner,
                                              leader_token=hub._leader_token)
    assert reader.read_fresh_many(requests, HOST) == [None]*56
    # Fail the second insert after the first row was written in the writer.
    with hub.source.shared.db() as db:
        db.execute("CREATE TRIGGER disk_failure BEFORE INSERT ON cache "
                   "WHEN (SELECT COUNT(*) FROM cache)>0 BEGIN "
                   "SELECT RAISE(ABORT, 'injected disk failure'); END")
    records = [hub._prepare_ws_event(*event) for event in events]
    with pytest.raises(CacheBusy, match='injected disk failure'):
        hub.source.shared.publish_websockets(records, leader_owner=hub.owner,
                                              leader_token=hub._leader_token)
    assert reader.read_fresh_many(requests, HOST) == [None]*56
    assert not hub.source.shared._connection.in_transaction
    with hub.source.shared.db() as db:
        db.execute('DROP TRIGGER disk_failure')
    assert all(hub.publish_ws_events(events))


def test_missing_mark_does_not_hide_other_symbols_or_fabricate_collateral(cohort):
    hub, reader, queue, requests, _ = cohort
    hub.source._quote_stream.disconnect()
    outcomes = hub.publish_ws_events(queue.take_batch(timeout=0))
    assert outcomes.count(True) == outcomes.count(False) == 28
    rows = reader.read_fresh_many(requests, HOST)
    assert all(row is None for row in rows[::2])
    assert all(row is not None for row in rows[1::2])


def test_queue_batch_preserves_fair_order_coalescing_and_bound():
    queues = [MarketEventQueue(urgent_burst=3) for _ in range(2)]
    hot = {('tickers', f'H{i}USDT') for i in range(70)}
    cold = {('depth50', 'SCANUSDT'), ('tickers', 'SCANUSDT')}
    for queue in queues:
        queue.configure(hot | cold, hot)
        for item in sorted(hot | cold):
            queue.notify(*item)
            queue.notify(*item)
    batch = queues[0].take_batch(timeout=0)
    assert len(batch) == 64 and set(batch) >= cold
    assert batch == [queues[1].take(timeout=0) for _ in range(64)]
    assert len(queues[0].take_batch(timeout=0)) == 8
    assert queues[0].take_batch(timeout=0) == []
    with pytest.raises(ValueError):
        queues[0].take_batch(limit=65)
