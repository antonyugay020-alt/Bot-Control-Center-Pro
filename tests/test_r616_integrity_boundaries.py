"""Regression for damaged control metadata, leader ownership and safe restart."""

import json
import logging
import time

import pytest

from bcc.control import stop_requested
from bcc.hub_supervisor import HubUnavailable, ensure_hub
from bcc.market_hub import HubRegistry
from bcc.storage import atomic_json


@pytest.mark.parametrize('broken', ['[]', 'null', '42', '"junk"'])
def test_malformed_hub_process_json_recovers_when_there_is_no_leader(tmp_path, broken):
    path = tmp_path / 'market_hub_process.json'
    path.write_text(broken, encoding='utf-8')
    pid = ensure_hub(tmp_path, timeout=3)
    from bcc import process_os as psutil
    try:
        assert pid > 0
        assert json.loads(path.read_text(encoding="utf-8"))['pid'] == pid
        assert ensure_hub(tmp_path, timeout=3) == pid
    finally:
        process = psutil.Process(pid)
        process.terminate()
        process.wait(timeout=5)


def test_corrupt_hub_identity_never_clobbers_an_active_leader(tmp_path, monkeypatch):
    record_path = tmp_path / 'market_hub_process.json'
    record_path.write_text('[]', encoding='utf-8')
    registry = HubRegistry(tmp_path / 'market_cache.sqlite')
    assert registry.claim_leader('999999:existing', lifetime=90)
    from bcc import hub_supervisor
    starts = []
    monkeypatch.setattr(hub_supervisor, '_spawn_hub', lambda *a, **k: starts.append(1))
    with pytest.raises(HubUnavailable, match='identity'):
        ensure_hub(tmp_path, timeout=.2)
    assert not starts
    assert record_path.read_text(encoding="utf-8") == '[]'
    assert registry.claim_leader('new-leader', lifetime=90) is False


@pytest.mark.parametrize('broken', ['[]', 'null', '42', '"junk"', '{corrupt'])
def test_malformed_stop_request_cannot_stop_or_crash_worker(tmp_path, broken, caplog):
    request = tmp_path / 'stop.request'
    request.write_text(broken, encoding='utf-8')
    with caplog.at_level(logging.ERROR):
        assert stop_requested(tmp_path, 'live-token') is False
    assert request.is_file()  # operator can replace it atomically


def test_stop_request_requires_real_identity_and_honours_current_token(tmp_path):
    path = tmp_path / 'stop.request'
    for value in [{}, {'id': ''}, {'token': None}, {'token': 99}, {'token': 'live-token'}, {'id': 'old', 'token': 'previous'}]:
        atomic_json(path, value)
        assert not stop_requested(tmp_path, 'live-token')
    atomic_json(path, {'id': 'new', 'token': 'live-token'})
    assert stop_requested(tmp_path, 'live-token')
    atomic_json(path, {'id': 'legacy'})
    assert stop_requested(tmp_path, 'live-token')
