"""Regression: a follower must not time out before the owner's allowed HTTP GET."""
import threading
import time
from bcc.market import PublicData
from bcc.data_cache import PublicCache


def test_follower_waits_for_slow_but_valid_http_owner(tmp_path):
    path = tmp_path / 'shared.sqlite'
    started = threading.Event()
    class Reply:
        status_code = 200
        headers = {}
        def raise_for_status(self): pass
        def json(self): return {'valid': True}
    class SlowSession:
        def get(self, *args, **kwargs):
            started.set()
            time.sleep(9.4)  # Still within the HTTP connect/read timeout budget.
            return Reply()
        def close(self): pass
    leader, follower = PublicData(), PublicData()
    leader.shared, follower.shared = PublicCache(path), PublicCache(path)
    result = {}
    def run():
        leader._thread.session = SlowSession()
        try: result['leader'] = leader.get('https://api.bybit.com/v5/market/kline', {'symbol': 'BTCUSDT'}, ttl=20)
        except Exception as exc: result['leader_error'] = repr(exc)
    task = threading.Thread(target=run)
    task.start()
    try:
        assert started.wait(timeout=3)
        result['follower'] = follower.get('https://api.bybit.com/v5/market/kline', {'symbol': 'BTCUSDT'}, ttl=20)
        task.join(timeout=4)
        assert not task.is_alive()
        assert result == {'leader': {'valid': True}, 'follower': {'valid': True}}
    finally:
        task.join(timeout=15)
        leader.close()
        follower.close()
