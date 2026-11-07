"""Independent failure-path regression for startup, UI preferences and Hub."""

import json
import os
import subprocess
import sys
import time

import pytest

from bcc.config import defaults
from bcc.hub_supervisor import HubUnavailable, ensure_hub
from bcc.launch_claim import claim_path, verify_worker_token
from bcc.lifecycle import Lifecycle
from bcc.preferences import DEFAULT_PREFERENCES, normalize_preferences
from bcc.storage import BotBusy, Repository


@pytest.mark.parametrize("untrusted", [None, [], "wrong", 1, {},
    {"scale": True, "refresh_ms": -10, "theme": "unknown"},
    {"scale": 100000, "refresh_ms": 1, "theme": None},
])
def test_broken_optional_preferences_cannot_crash_dashboard(untrusted):
    assert normalize_preferences(untrusted) == DEFAULT_PREFERENCES


def test_valid_settings_survive_and_unknown_fields_do_not(tmp_path):
    good = {"scale": 125, "refresh_ms": 1000, "theme": "light", "private_key": "untrusted"}
    result = normalize_preferences(good)
    assert result == {"scale": 125, "refresh_ms": 1000, "theme": "light"}
    assert normalize_preferences({"theme": "light"})["theme"] == "light"


def test_hub_pid_without_live_leader_lease_never_passes_as_ready(tmp_path, monkeypatch):
    import bcc.hub_supervisor as supervisor

    (tmp_path / 'market_hub_process.json').write_text(json.dumps({
        'pid': os.getpid(), 'created': time.time(),
    }), encoding="utf-8")
    monkeypatch.setattr(supervisor, '_matches', lambda record, cache: True)
    monkeypatch.setattr(supervisor, '_spawn_hub', lambda *args, **kwargs:
                        pytest.fail('must not spawn a duplicate while PID is still alive'))
    with pytest.raises(HubUnavailable, match='no valid leadership lease'):
        ensure_hub(tmp_path, timeout=.5)
    from bcc.market_hub import HubRegistry
    registry = HubRegistry(tmp_path / 'market_cache.sqlite')
    with registry._connect() as db:
        db.execute('INSERT INTO hub_leader(singleton,owner,expires) VALUES (1,?,?)',
                   (f'{os.getpid()}:stale', time.time() - 100))
    with pytest.raises(HubUnavailable, match='no valid leadership lease'):
        ensure_hub(tmp_path, timeout=.5)


def test_missing_launch_claim_cannot_authorize_orphaned_worker(tmp_path):
    repo = Repository(tmp_path)
    cfg = defaults('POLYMARKET'); cfg['name'] = 'Startup integrity'
    folder = repo.create(cfg)
    with pytest.raises(BotBusy, match='не принадлежит'):
        verify_worker_token(folder, 'revoked')


def test_launcher_registration_failure_revokes_and_reaps_process(tmp_path, monkeypatch):
    repo = Repository(tmp_path)
    cfg = defaults('POLYMARKET'); cfg['name'] = 'Startup integrity'
    folder = repo.create(cfg)
    import bcc.lifecycle as lifecycle
    real_popen = subprocess.Popen
    spawned = []

    def spawn(*args, **kwargs):
        child = real_popen([sys.executable, '-c', 'import time;time.sleep(30)'], **kwargs)
        spawned.append(child)
        return child

    monkeypatch.setattr(lifecycle.subprocess, 'Popen', spawn)
    monkeypatch.setattr(lifecycle, 'register_launcher', lambda *args:
                        (_ for _ in ()).throw(OSError('cannot persist worker registration')))
    try:
        with pytest.raises(OSError, match='cannot persist worker registration'):
            Lifecycle(repo).start(folder)
        assert len(spawned) == 1
        assert spawned[0].poll() is not None, 'worker launcher was abandoned'
        assert not claim_path(folder).exists(), 'orphan worker could still authenticate'
    finally:
        for child in spawned:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
