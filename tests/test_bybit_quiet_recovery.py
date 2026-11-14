"""Independent clocks and quiet-symbol recovery through genuine WS events."""
import json

import pytest

from bcc.ws_quotes import BybitQuoteStream, StreamUnavailable


class Socket:
    def __init__(self):
        self.sent = []
        self.closed = False

    def send(self, data):
        self.sent.append(json.loads(data))

    def close(self):
        self.closed = True


def setup():
    clock = [1763000000., 100.]
    socket = Socket()
    stream = BybitQuoteStream(wall=lambda: clock[0], monotonic=lambda: clock[1])
    stream.subscribe('ADAUSDT')
    generation = stream.connect(socket)
    return clock, socket, stream, generation


def feed(stream, generation, stamp, kind='delta', **fields):
    return stream.ingest({'type': kind, 'topic': 'tickers.ADAUSDT',
                          'ts': int(stamp * 1000), 'data': fields}, generation=generation)


def advance(clock, seconds):
    clock[0] += seconds
    clock[1] += seconds


def test_quiet_mark_recovery_requests_new_snapshot_without_renewing_old_evidence():
    c, socket, stream, gen = setup()
    assert feed(stream, gen, c[0], 'snapshot', bid1Price='.4', ask1Price='.41', markPrice='.405')
    advance(c, 3)
    assert stream.refresh_stale_marks(['ADAUSDT']) == 1
    assert socket.sent == [
        {'op': 'unsubscribe', 'args': ['tickers.ADAUSDT']},
        {'op': 'subscribe', 'args': ['tickers.ADAUSDT']}]
    with pytest.raises(StreamUnavailable):
        stream.mark('ADAUSDT')
    assert stream.refresh_stale_marks(['ADAUSDT']) == 0
    # Only a real response restores the collateral observation.
    assert feed(stream, gen, c[0], 'snapshot', bid1Price='.4', ask1Price='.41', markPrice='.405')
    for _ in range(24):
        advance(c, 1)
        assert stream.refresh_stale_marks(['ADAUSDT']) == 1
        # Proactive recovery: previous observation is still genuinely fresh.
        assert stream.mark('ADAUSDT')[1] == pytest.approx(c[0] - 1)
        assert feed(stream, gen, c[0], 'snapshot', bid1Price='.4', ask1Price='.41', markPrice='.405')
        assert stream.mark('ADAUSDT')[1] == pytest.approx(c[0])


def test_stale_side_recovers_on_next_partial_delta_without_new_snapshot():
    c, _, stream, gen = setup()
    assert feed(stream, gen, c[0], 'snapshot', bid1Price='.4', ask1Price='.41')
    advance(c, 2.1)
    assert not feed(stream, gen, c[0], bid1Price='.401')
    with pytest.raises(StreamUnavailable):
        stream.quote('ADAUSDT')
    advance(c, .1)
    assert feed(stream, gen, c[0], ask1Price='.411')
    assert stream.quote('ADAUSDT')[:2] == (.401, .411)


def test_newer_quote_does_not_discard_independently_ordered_mark():
    c, _, stream, gen = setup()
    assert feed(stream, gen, c[0], 'snapshot', bid1Price='.4', ask1Price='.41', markPrice='.405')
    advance(c, 1)
    assert feed(stream, gen, c[0], bid1Price='.401')
    assert feed(stream, gen, c[0] - .5, markPrice='.406')
    assert stream.mark('ADAUSDT')[0] == .406
    # Replayed marks cannot refresh their independent receipt clock.
    advance(c, 1.1)
    assert not feed(stream, gen, c[0] - 1.6, markPrice='.406')


def test_mark_only_delta_not_suppressed_by_expired_other_quote_side():
    c, _, stream, gen = setup()
    assert feed(stream, gen, c[0], 'snapshot', bid1Price='.4', ask1Price='.41')
    advance(c, 2.1)
    assert feed(stream, gen, c[0], bid1Price='.401', markPrice='.405')
    assert stream.mark('ADAUSDT')[0] == .405
    with pytest.raises(StreamUnavailable):
        stream.quote('ADAUSDT')


def test_failed_recovery_closes_owned_socket_and_revokes_generation():
    c, socket, stream, gen = setup()
    advance(c, 3)
    def broken(data):
        raise OSError('socket closed')
    socket.send = broken
    with pytest.raises(StreamUnavailable, match='refresh'):
        stream.refresh_stale_marks(['ADAUSDT'])
    assert socket.closed
    assert not feed(stream, gen, c[0], markPrice='.405')


def test_fresh_mark_does_not_hide_expiring_bid_ask_from_snapshot_recovery():
    c, socket, stream, gen = setup()
    advance(c, 3)
    assert feed(stream, gen, c[0], 'snapshot', bid1Price='.4', ask1Price='.41', markPrice='.405')
    advance(c, 1.1)
    assert feed(stream, gen, c[0], markPrice='.406')
    assert stream.refresh_stale_marks(['ADAUSDT']) == 1
    assert len(socket.sent) == 2
    assert stream.mark('ADAUSDT')[0] == .406


def test_initial_grace_and_recovery_batching_remain_bounded_for_fleet():
    c, socket, stream, _ = setup()
    symbols = [f'COIN{i}USDT' for i in range(120)]
    stream.subscribe_many(symbols)
    socket.sent.clear()
    assert stream.refresh_stale_marks(symbols) == 0
    advance(c, 3)
    assert stream.refresh_stale_marks(symbols) == 120
    assert len(socket.sent) == 24
    assert all(len(row['args']) == 10 for row in socket.sent)
    assert stream.refresh_stale_marks(symbols) == 0


def test_first_exchange_snapshot_ends_handshake_grace_before_mark_expires():
    c, socket, stream, gen = setup()
    assert feed(stream, gen, c[0], 'snapshot', bid1Price='.4', ask1Price='.41', markPrice='.405')
    advance(c, 1.1)
    assert stream.refresh_stale_marks(['ADAUSDT']) == 1
    assert len(socket.sent) == 2
    assert stream.mark('ADAUSDT')[0] == .405


def test_independent_quiet_mark_recovers_through_real_hub_sqlite(tmp_path, monkeypatch):
    from bcc import market_clock
    from bcc.market import PublicData, DataUnavailable
    from bcc.market_hub import MarketDataHub
    c, _, stream, gen = setup()
    monkeypatch.setattr(market_clock, 'utc_bounds', lambda: (c[0], c[0]))
    path = tmp_path / 'isolated_cache.sqlite'
    url = 'https://api.bybit.com/v5/market/tickers'
    params = {'category': 'linear', 'symbol': 'ADAUSDT'}
    producer = PublicData(quote_stream=stream)
    hub = MarketDataHub(path, source=producer)
    reader = None
    try:
        hub.registry.subscribe(url, params)
        assert hub.acquire_leadership()
        assert feed(stream, gen, c[0], 'snapshot', bid1Price='.4', ask1Price='.41', markPrice='.405')
        assert hub.publish_ws_event('tickers', 'ADAUSDT')
        monkeypatch.setenv('BCC_PUBLIC_CACHE', str(path))
        monkeypatch.setenv('BCC_MARKET_HUB_MODE', 'client')
        monkeypatch.setenv('BCC_BYBIT_WS_QUOTES', '1')
        reader = PublicData()
        def forbidden(*args, **kwargs):
            raise AssertionError('REST must never replace an independent WS observation')
        monkeypatch.setattr(reader.session, 'get', forbidden)
        assert reader._ws_position_mark('ADAUSDT')['bid'] == .405
        advance(c, 2.1)
        # Updated bid plus real mark; the independent untouched ask expired.
        assert feed(stream, gen, c[0], bid1Price='.401', markPrice='.406')
        assert hub.publish_ws_event('tickers', 'ADAUSDT')
        assert reader._ws_position_mark('ADAUSDT')['bid'] == .406
        with pytest.raises(DataUnavailable, match='WebSocket'):
            reader.get(url, params)
        stream.disconnect()
        assert not hub.publish_ws_event('tickers', 'ADAUSDT')
        gen = stream.connect()
        assert feed(stream, gen, c[0], 'snapshot', bid1Price='.401', ask1Price='.411', markPrice='.407')
        assert hub.publish_ws_event('tickers', 'ADAUSDT')
        assert reader._ws_position_mark('ADAUSDT')['bid'] == .407
        assert reader.get(url, params)['_bcc_ws_quote'] is True
    finally:
        if reader is not None:
            reader.close()
        hub.close()
        producer.close()
