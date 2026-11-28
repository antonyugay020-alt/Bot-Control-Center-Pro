"""Verify the isolated WS delivery policy without sending wire traffic."""
import pytest

from bcc import market_clock, ws_split_ticker
from bcc.data_cache import CacheContention
from bcc.ws_sharded_quotes import StablePair
from test_ws_atomic_publication_batch import cohort
from test_ws_sharded_integration import Socket, frame

SYMBOL = 'AUSDT'


@pytest.fixture
def pair(monkeypatch):
    now = [100.]
    bounds = lambda stamp: (now[0] - stamp, now[0] - stamp + .05)
    monkeypatch.setattr(market_clock, 'book_age_bounds', bounds)
    monkeypatch.setattr(market_clock, 'latest_now', lambda: now[0] + .05)
    monkeypatch.setattr(ws_split_ticker, 'book_age_bounds', bounds)
    stream = StablePair(refresh_quiet=True, wall=lambda: now[0], monotonic=lambda: now[0])
    primary, reserve = Socket(), Socket()
    stream.sync_symbols([SYMBOL])
    stream._primary.connect(primary)
    stream._recovery.connect(reserve)
    assert frame(stream._recovery, SYMBOL, now[0], markPrice='100')
    try:
        yield now, stream, reserve
    finally:
        stream.stop()


def ack(reserve, stream, op):
    message = reserve.sent[-1]
    assert message['op'] == op
    child = stream._recovery
    assert child._handle_control_message(
        reserve, dict(op=op, req_id=message['req_id'], success=True), child._generation)


def test_quiet_snapshot_replaces_only_on_genuine_evidence_and_matching_ack(pair):
    now, stream, reserve = pair
    child = stream._recovery
    original = child.maintained_mark(SYMBOL)
    assert stream.refresh_stale_marks([SYMBOL]) == 0
    now[0] += 1.5
    assert stream.refresh_stale_marks([SYMBOL]) == 1
    assert stream.refresh_stale_marks([SYMBOL]) == 0
    assert child.maintained_mark(SYMBOL) == original
    assert stream.transport_health()['recovery']['received_symbols'] == 1
    ack(reserve, stream, 'unsubscribe')
    # Bybit's genuine snapshot can predate the wire command.
    assert frame(child, SYMBOL, 101., cs=2, markPrice='101')
    assert child.maintained_mark(SYMBOL) == original
    ack(reserve, stream, 'subscribe')
    assert child.maintained_mark(SYMBOL)[:2] == (101., 101.)
    child.disconnect()
    assert child.maintained_mark(SYMBOL) is None
    assert stream.transport_health()['recovery']['received_symbols'] == 0


def test_duplicate_snapshot_and_empty_ack_never_extend_original_expiry(pair):
    now, stream, reserve = pair
    child = stream._recovery
    original = child.maintained_mark(SYMBOL)
    now[0] += 1.5
    assert stream.refresh_stale_marks([SYMBOL]) == 1
    ack(reserve, stream, 'unsubscribe')
    assert not frame(child, SYMBOL, 100., markPrice='101')
    assert frame(child, SYMBOL, 100., markPrice='100')
    ack(reserve, stream, 'subscribe')
    assert child.maintained_mark(SYMBOL) == original
    # The original snapshot is also enough to resume genuine ordered deltas.
    assert child.chain_initialized(SYMBOL)
    now[0] += .5
    assert stream.refresh_stale_marks([SYMBOL]) == 1
    ack(reserve, stream, 'unsubscribe')
    ack(reserve, stream, 'subscribe')
    assert child.maintained_mark(SYMBOL) == original
    assert child.refresh_pressure()['ack_without_fresh_mark'] == 1
    now[0] = 103.01
    assert child.maintained_mark(SYMBOL) is None


def test_native_publication_retries_transient_writer_contention_from_live_state(cohort, monkeypatch):
    hub, _, queue, _, _ = cohort
    hub.set_ws_dispatcher(queue, retry_contention=True)
    batch = queue.take_batch(timeout=0)
    cache = hub.source.shared
    with monkeypatch.context() as scoped:
        def busy(*args, **kwargs):
            raise CacheContention('temporary SQLite gate contention')
        scoped.setattr(cache, 'publish_websockets', busy)
        assert not any(hub.publish_ws_events(batch))
    replay = queue.take_batch(timeout=0)
    assert set(replay) == set(batch)
    assert all(hub.publish_ws_events(replay))


def test_native_transport_readiness_requires_books_and_both_ticker_lanes():
    from bcc.ws_orderbooks import BybitOrderbookWS
    from bcc.ws_sharded_quotes import ShardedBybitQuoteStream
    from tools.ws_native_profile import transport_ready

    quotes = ShardedBybitQuoteStream()
    books = BybitOrderbookWS()
    try:
        books.connect(Socket())
        for shard in quotes._pairs:
            shard._primary.connect(Socket())
            shard._recovery.connect(Socket())
        assert transport_ready(quotes, books)
        assert not books._symbols and not quotes._demand
        quotes._pairs[-1]._recovery.disconnect()
        assert not transport_ready(quotes, books)
        quotes._pairs[-1]._recovery.connect(Socket())
        books.disconnect()
        assert not transport_ready(quotes, books)
    finally:
        quotes.stop()
        books.stop()


def test_native_transport_wait_checks_owned_receipt_and_is_bounded(tmp_path):
    import json

    from tools.ws_native_profile import NATIVE_WS_READY, wait_transport_ready

    with pytest.raises(TimeoutError, match='connections did not become ready'):
        wait_transport_ready(tmp_path, 123, timeout=0)
    path = tmp_path / NATIVE_WS_READY
    receipt = dict(pid=123, ticker_connections=8, book_connections=1)
    path.write_text(json.dumps(receipt), encoding='utf-8')
    assert wait_transport_ready(tmp_path, 123) == receipt
    with pytest.raises(ValueError, match='owned Hub'):
        wait_transport_ready(tmp_path, 456)


def test_preserve_staged_snapshot_during_other_topic_refresh_on_same_reserve(pair):
    now, stream, reserve = pair
    child = stream._recovery
    other = 'ADAUSDT'
    stream.sync_symbols([SYMBOL, other])
    assert frame(child, other, now[0], markPrice='200')
    now[0] = 101.5
    assert child.request_quiet_snapshots([SYMBOL]) == (SYMBOL,)
    ack(reserve, stream, 'unsubscribe')
    staged_subscribe = reserve.sent[-1]
    assert child.request_quiet_snapshots([other]) == (other,)
    assert SYMBOL in child._maintained.active
    assert frame(child, SYMBOL, 101., cs=2, markPrice='101')
    assert child.maintained_mark(SYMBOL)[:2] == (100., 100.)
    payload = dict(op='subscribe', req_id=staged_subscribe['req_id'], success=True)
    assert child._handle_control_message(reserve, payload, child._generation)
    assert child.maintained_mark(SYMBOL)[:2] == (101., 101.)
    assert other in child._wire_symbols
