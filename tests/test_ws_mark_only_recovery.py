"""Collateral recovery is independent of optional executable ticker fields."""
import json
import pytest

from bcc.ws_dual_quotes import DualBybitQuoteStream
from bcc.ws_quotes import StreamUnavailable
from bcc.ws_recovery import SNAPSHOT_RESPONSE_TIMEOUT
from bcc.ws_recovery_quotes import RecoveryQuoteStream


class Sink:
    def __init__(self):
        self.sent = []
        self.closed = False

    def send(self, data):
        self.sent.append(json.loads(data))

    def close(self):
        self.closed = True


def frame(child, now, symbol='BTCUSDT', kind='delta', **data):
    return child.ingest(dict(type=kind, topic='tickers.' + symbol,
                             ts=round(now * 1000), data=data),
                        generation=child._generation)


def setup(names=('BTCUSDT',)):
    now = [10000.]
    stream = DualBybitQuoteStream(wall=lambda: now[0], monotonic=lambda: now[0])
    primary, reserve = Sink(), Sink()
    stream._primary.connect(primary)
    stream._recovery.connect(reserve)
    stream.sync_symbols(list(names))
    stream.set_priority_symbols(list(names))
    return now, stream, primary, reserve


def ack(child, sock, start):
    index = start
    while index < len(sock.sent):
        msg = sock.sent[index]
        index += 1
        if 'req_id' in msg:
            assert child._handle_control_message(sock, dict(op=msg['op'],
                req_id=msg['req_id'], success=True), child._generation)


def test_mark_only_delta_completes_only_mark_recovery():
    now, stream, primary, reserve = setup()
    child = stream._recovery
    assert frame(stream._primary, now[0], markPrice='100')
    now[0] += 1.1
    assert stream.refresh_stale_marks(['BTCUSDT']) == 1
    assert frame(child, now[0], markPrice='100.1')
    now[0] += 1.1
    start = len(reserve.sent)
    assert stream.refresh_stale_marks(['BTCUSDT']) == 1
    ack(child, reserve, start)
    before = child.refresh_pressure()
    assert before['pending_snapshot_topics'] == 1
    assert frame(child, now[0], bid1Price='99', ask1Price='101')
    assert child.refresh_pressure()['pending_snapshot_topics'] == 1
    assert not frame(child, now[0] - 1.1, markPrice='100.1')
    assert child.refresh_pressure()['pending_snapshot_topics'] == 1
    now[0] += .05
    assert frame(child, now[0], markPrice='100.2')
    assert child.refresh_pressure()['pending_snapshot_topics'] == 0
    assert child.refresh_pressure()['recovered_snapshot_topics'] == 1
    assert child.mark('BTCUSDT')[1] == round(now[0] * 1000) / 1000
    assert len(primary.sent) == 1


def test_either_lane_fresh_mark_prevents_quote_driven_commands():
    now, stream, primary, reserve = setup()
    # No quote on either child: the mark is independent from depth50.
    assert frame(stream._primary, now[0], markPrice='100')
    now[0] += .8
    assert stream.refresh_stale_marks(['BTCUSDT']) == 0
    assert not reserve.sent
    now[0] += .3
    assert stream.refresh_stale_marks(['BTCUSDT']) == 1
    assert frame(stream._recovery, now[0], markPrice='100.1')
    now[0] += .7
    assert stream.refresh_stale_marks(['BTCUSDT']) == 0
    assert len(reserve.sent) == 1
    assert frame(stream._primary, now[0], markPrice='100.2')
    now[0] += .8
    assert stream.refresh_stale_marks(['BTCUSDT']) == 0
    assert len(reserve.sent) == 1
    assert len(primary.sent) == 1


def test_initial_reserve_without_mark_retries_before_consumer_expiry():
    now, stream, primary, reserve = setup()
    assert frame(stream._primary, now[0], markPrice='100')
    now[0] += 1.1
    assert stream.refresh_stale_marks(['BTCUSDT']) == 1
    now[0] += SNAPSHOT_RESPONSE_TIMEOUT - .05
    assert stream.refresh_stale_marks(['BTCUSDT']) == 0
    now[0] += .1
    assert stream.refresh_stale_marks(['BTCUSDT']) == 1
    assert reserve.sent[-1]['op'] == 'unsubscribe'
    assert now[0] - 10000. < 3.
    assert len(primary.sent) == 1


def test_mark_only_recovery_no_response_and_ack_never_extend_price():
    now = [10000.]
    sock = Sink()
    child = RecoveryQuoteStream(wall=lambda: now[0], monotonic=lambda: now[0], mark_only_recovery=True)
    child.connect(sock)
    child.subscribe('BTCUSDT')
    child.set_priority_symbols(['BTCUSDT'])
    assert frame(child, now[0], markPrice='100')
    now[0] += .6
    start = len(sock.sent)
    assert child.refresh_stale_marks(['BTCUSDT']) == 1
    ack(child, sock, start)
    for _ in range(5):
        now[0] += .25
        frame(child, now[0], bid1Price='99', ask1Price='101')
        child.refresh_stale_marks(['BTCUSDT'])
    now[0] += 1.2
    with pytest.raises(StreamUnavailable):
        child.mark('BTCUSDT')
    assert child.refresh_pressure()['recovered_snapshot_topics'] == 0


def test_raw_health_reports_explicit_mark_age_without_quotes():
    now, stream, _, _ = setup()
    assert frame(stream._primary, now[0], markPrice='100')
    now[0] += 3.2
    lane = stream.transport_health()['primary']
    assert lane['explicit_mark_symbols'] == 1
    assert lane['received_symbols'] == 0
    assert lane['explicit_mark_age_over_3s'] == 1
    assert lane['explicit_mark_max_age_seconds'] == pytest.approx(3.2)
    stream._primary.disconnect()
    assert stream.transport_health()['primary']['explicit_mark_symbols'] == 0


def test_500_symbols_24_rounds_with_delayed_explicit_marks(monkeypatch):
    """Synthetic provider sends real-format partial mark frames, not full tickers.

    500 active priority symbols, bounded polls and a delayed mark-only response
    per subscription. Test checks the scheduler, not exchange capacity.
    """
    names = [f'Q{i}USDT' for i in range(500)]
    now, stream, primary, reserve = setup(names)
    from bcc import ws_split_ticker
    monkeypatch.setattr(ws_split_ticker, 'book_age_bounds',
                        lambda stamp: (now[0] - stamp, now[0] - stamp + .1))
    for name in names:
        assert frame(stream._primary, now[0], symbol=name, markPrice='100')
    original_primary = list(primary.sent)
    child = stream._recovery
    handled = 0
    due = []
    for turn in range(192):
        now[0] += .25
        for symbol, stamp in due:
            assert frame(child, stamp, symbol=symbol, markPrice='100.1')
        due = []
        assert stream.refresh_stale_marks(names) <= 128
        # Fake provider ACKs and returns explicit mark after a bounded delay.
        # Never complete a pending recovery by merely returning a wire ACK.
        while handled < len(reserve.sent):
            msg = reserve.sent[handled]
            handled += 1
            if 'req_id' in msg:
                assert child._handle_control_message(reserve, dict(op=msg['op'],
                    req_id=msg['req_id'], success=True), child._generation)
            if msg['op'] == 'subscribe':
                due.extend((topic.split('.', 1)[1], now[0] + .1)
                           for topic in msg['args'])
        if (turn + 1) % 8 == 0:
            for name in names:
                mark, quote = stream.independent_observations_for_hub(name)
                assert mark is not None and quote is None, (turn, name)
                assert now[0] - mark[1] + .1 < 3.
    assert primary.sent == original_primary
    assert child.refresh_pressure()['snapshot_response_timeouts'] == 0
