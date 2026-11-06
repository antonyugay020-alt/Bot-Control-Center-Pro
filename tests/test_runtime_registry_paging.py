from bcc.coordination import Coordinator
import pytest


def test_keyset_pagination_preserves_fairness_at_any_registry_size(tmp_path):
    c = Coordinator(tmp_path / 'coordination.sqlite')
    c.initialize()
    for i in range(137):
        c.register('bot%04d' % i)
    seen = set()
    cursor = None
    for _ in range(20):
        keys = c.next_keys(cursor, limit=7)
        assert 1 <= len(keys) <= 7
        seen.update(keys)
        cursor = keys[-1]
    assert len(seen) == 137
    assert c.next_keys('zzzz', limit=2) == ['bot0000', 'bot0001']
    with pytest.raises(ValueError):
        c.next_keys(None, limit=0)
