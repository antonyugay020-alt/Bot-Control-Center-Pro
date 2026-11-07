import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock
from bcc.market import PublicData, DataUnavailable
from bcc.data_cache import PublicCache

URL = 'https://api.bybit.com/v5/market/kline'
HOST = 'api.bybit.com'

class LocalCooldownCacheTests(unittest.TestCase):
    def test_memory_cache_remains_available_during_cooldown(self):
        source = PublicData()
        source.cache[(URL, '{}')] = (time.time(), {'ok': 1})
        source.cooldowns[HOST] = time.time() + 100
        source.session.get = Mock(side_effect=AssertionError('unexpected network'))
        self.assertEqual(source.get(URL, {}, ttl=10), {'ok': 1})
        source.close()

    def test_shared_cache_hit_is_allowed_but_miss_is_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = PublicData()
            source.shared = PublicCache(Path(tmp) / 'shared.sqlite')
            key, owner, _ = source.shared.begin(URL, {}, HOST)
            source.shared.finish(key, owner, HOST, {'ok': 2}, ttl=60)
            source.cooldowns[HOST] = time.time() + 100
            source.session.get = Mock(side_effect=AssertionError('unexpected network'))
            self.assertEqual(source.get(URL, {}, ttl=0), {'ok': 2})
            with self.assertRaises(DataUnavailable):
                source.get(URL, {'symbol': 'ETHUSDT'}, ttl=0)
            source.close()
