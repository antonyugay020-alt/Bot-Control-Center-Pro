"""Regression: market HTTP must never delay the hub readiness handshake."""
import json
import os
import sys

import pytest

from tools import market_hub_service


class Finished(Exception):
    pass


def test_child_writes_authenticated_receipt_before_processing_slow_queue(tmp_path, monkeypatch):
    receipt = tmp_path / 'receipt.json'
    token = 'd' * 32
    calls = []

    class Registry:
        def claim_leader(self, owner, *, lifetime):
            calls.append('lease')
            assert owner == 'owned-by-test' and lifetime == 120
            return True

    class Hub:
        owner = 'owned-by-test'
        registry = Registry()
        def __init__(self, cache, *, max_workers=1):
            assert cache == str(tmp_path / 'cache.sqlite')
            assert max_workers == 6
        def poll_nonblocking(self):
            # This stands in for a network queue that could block for a long
            # time. In the old service dispatched its blocking batch BEFORE the receipt.
            calls.append('market-requests')
            assert json.loads(receipt.read_text(encoding='utf-8')) == {
                'pid': os.getpid(), 'token': token,
            }
            raise Finished
        def close(self):
            calls.append('closed')

    monkeypatch.setattr(market_hub_service, 'MarketDataHub', Hub)
    monkeypatch.setattr(market_hub_service.signal, 'signal', lambda *a: None)
    monkeypatch.setattr(sys, 'argv', ['hub', '--cache', str(tmp_path / 'cache.sqlite'),
                                      '--ready-file', str(receipt), '--ready-token', token])
    with pytest.raises(Finished):
        market_hub_service.main()
    assert calls == ['lease', 'market-requests', 'closed']


def test_child_never_claims_readiness_while_leader_lease_is_denied(tmp_path, monkeypatch):
    receipt = tmp_path / 'receipt.json'
    attempts = []

    class Registry:
        def claim_leader(self, owner, *, lifetime):
            attempts.append(1)
            if len(attempts) == 1:
                return False
            raise Finished

    class Hub:
        owner = 'not-leader'
        registry = Registry()
        def __init__(self, cache, *, max_workers=1):
            assert max_workers == 6
        def poll_nonblocking(self):
            pytest.fail('Never fetch data without leadership')
        def close(self):
            pass

    monkeypatch.setattr(market_hub_service, 'MarketDataHub', Hub)
    monkeypatch.setattr(market_hub_service.signal, 'signal', lambda *a: None)
    monkeypatch.setattr(sys, 'argv', ['hub', '--cache', str(tmp_path / 'cache.sqlite'),
                                      '--ready-file', str(receipt), '--ready-token', 'c'*32,
                                      '--poll', '0.02'])
    with pytest.raises(Finished):
        market_hub_service.main()
    assert len(attempts) == 2
    assert not receipt.exists()
