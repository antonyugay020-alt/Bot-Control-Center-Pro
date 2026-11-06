import sqlite3
import time
import tempfile
import unittest
from pathlib import Path

from bcc.data_cache import PublicCache, CacheBusy


class CacheCooldownTests(unittest.TestCase):
    def test_valid_cached_response_survives_exchange_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = PublicCache(Path(tmp) / 'market.sqlite')
            host = 'api.bybit.com'
            url = 'https://api.bybit.com/v5/market/kline'
            params = {'symbol': 'BTCUSDT'}
            try:
                key, owner, value = cache.begin(url, params, host)
                self.assertIsNotNone(owner)
                cache.finish(key, owner, host, value={'retCode': 0, 'result': {'list': []}}, ttl=60)
                with cache.db() as db:
                    db.execute('INSERT OR REPLACE INTO cooldown VALUES(?,?)', (host, time.time() + 120))
                cached_key, cached_owner, cached_value = cache.begin(url, params, host)
                self.assertEqual(key, cached_key)
                self.assertIsNone(cached_owner)
                self.assertEqual(cached_value['retCode'], 0)
                with self.assertRaises(CacheBusy):
                    cache.begin(url, {'symbol': 'ETHUSDT'}, host)
            finally:
                cache.close()
