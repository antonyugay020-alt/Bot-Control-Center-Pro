"""Regressions from native Windows QA 2026-11-02 (R6.18)."""
import os
import sqlite3
import subprocess
import sys

import pytest

from bcc.hub_supervisor import HubUnavailable, ensure_hub, _hub_startup_details
from bcc.market_hub import HubRegistry


def test_hub_registry_reuses_existing_wal_without_reconfiguration(tmp_path, monkeypatch):
    cache = tmp_path / 'market_cache.sqlite'
    HubRegistry(cache)
    original = HubRegistry._connect
    commands = []

    from contextlib import contextmanager

    @contextmanager
    def instrumented(self):
        # _connect now owns and closes its SQLite handle on context exit.
        # The old test treated the context manager itself as a connection.
        with original(self) as db:
            db.set_trace_callback(commands.append)
            yield db

    monkeypatch.setattr(HubRegistry, '_connect', instrumented)
    HubRegistry(cache)
    assert not any('journal_mode=WAL' in x for x in commands)
    assert any('PRAGMA journal_mode' in x for x in commands)


def test_hub_timeout_reports_own_log_and_reaps_child(tmp_path, monkeypatch):
    import bcc.hub_supervisor as supervisor
    spawned = []

    def no_start(_args, **kwargs):
        # An independent child that never takes the hub leader lease.
        proc = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'], **kwargs)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(supervisor, '_spawn_hub', no_start)
    try:
        with pytest.raises(HubUnavailable, match='did not become ready: pid=') as error:
            ensure_hub(tmp_path, timeout=.25)
        assert 'last_log=' in str(error.value)
        assert len(spawned) == 1 and spawned[0].poll() is not None
        assert not (tmp_path / 'market_hub_process.json').exists()
    finally:
        for child in spawned:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)


def test_hub_launch_clears_client_only_environment(tmp_path, monkeypatch):
    import bcc.hub_supervisor as supervisor
    monkeypatch.setenv('BCC_MARKET_HUB_MODE', 'client')
    monkeypatch.setenv('BCC_PUBLIC_CACHE', '/different/path')
    captured = []
    launched = []

    def child(_args, **kwargs):
        captured.append(kwargs['env'])
        p = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'], **kwargs)
        launched.append(p)
        return p

    monkeypatch.setattr(supervisor, '_spawn_hub', child)
    try:
        with pytest.raises(HubUnavailable):
            ensure_hub(tmp_path, timeout=.2)
        assert captured and 'BCC_MARKET_HUB_MODE' not in captured[0]
        assert 'BCC_PUBLIC_CACHE' not in captured[0]
        assert launched[0].poll() is not None
    finally:
        for child in launched:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)


def test_hub_error_log_tail_is_bounded(tmp_path):
    class Fake:
        pid = 901
        def poll(self):
            return None

    (tmp_path / 'market_hub.log').write_text('a' * 20000 + 'END', encoding='utf-8')
    result = _hub_startup_details(tmp_path, Fake())
    assert 'END' in result
    assert len(result) < 2500
