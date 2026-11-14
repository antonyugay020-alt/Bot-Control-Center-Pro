"""True independent-process smoke regression for the shared SQLite public-data lease."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from bcc.runtime_paths import python_environment


def test_fifteen_independent_processes_share_one_fetch(tmp_path):
    work = Path(tmp_path)
    worker = work / 'worker.py'
    worker.write_text('''import os, pathlib, time, json\nfrom bcc.market import PublicData\nfrom bcc.data_cache import PublicCache\nclass Response:\n status_code=200\n headers={}\n def raise_for_status(self): pass\n def json(self): return {"ok": True}\nclass Session:\n def get(self, *args, **kwargs):\n  with open(os.environ["FETCH_LOG"], "a") as f: f.write("fetch\\n")\n  time.sleep(0.5)\n  return Response()\n def close(self): pass\nstart=pathlib.Path(os.environ["START_GATE"])\nwhile not start.exists(): time.sleep(0.01)\ns=PublicData()\ns.shared=PublicCache(os.environ["SHARED_DB"])\ns._thread.session=Session()\ntry:\n value=s.get("https://api.bybit.com/v5/market/kline", {"symbol":"BTCUSDT"}, ttl=5)\n assert value=={"ok": True}, value\nfinally: s.close()\n''', encoding='utf-8')
    env = dict(os.environ, SHARED_DB=str(work / 'shared.sqlite'),
               FETCH_LOG=str(work / 'fetches.log'), START_GATE=str(work / 'go'))
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1]) + os.pathsep + env.get('PYTHONPATH', '')
    children = [subprocess.Popen([sys.executable, str(worker)], cwd=work, env=python_environment(env),
               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8') for _ in range(15)]
    try:
        (work / 'go').touch()
        errors = []
        for child in children:
            out, err = child.communicate(timeout=20)
            if child.returncode:
                errors.append((child.returncode, err[-500:]))
        assert not errors, errors
        assert (work / 'fetches.log').read_text(encoding="utf-8").splitlines() == ['fetch']
    finally:
        for child in children:
            if child.poll() is None: child.kill()
            child.communicate()


def test_sqlite_contention_retries_full_admission_not_direct_http(tmp_path, monkeypatch):
    """Transient WAL contention may retry, but only through authoritative begin."""
    from bcc.data_cache import CacheContention, PublicCache
    from bcc.market import PublicData

    shared = PublicCache(tmp_path / "contention.sqlite")
    source = PublicData()
    source.shared = shared
    try:
        begin = shared.begin
        attempts = []
        def busy_then_admit(*args, **kwargs):
            attempts.append(1)
            if len(attempts) <= 3:
                raise CacheContention("Общий кэш: database is locked")
            return begin(*args, **kwargs)

        monkeypatch.setattr(shared, "begin", busy_then_admit)
        seen = []
        class Response:
            status_code = 200
            headers = {}
            def raise_for_status(self):
                pass
            def json(self):
                return {"ok": True}

        class Session:
            def get(self, *args, **kwargs):
                seen.append(1)
                return Response()
            def close(self):
                pass

        source._thread.session = Session()
        result = source.get(
            "https://api.bybit.com/v5/market/kline",
            {"symbol": "BTCUSDT"}, ttl=5,
        )
        assert result == {"ok": True}
        assert len(attempts) == 4
        assert len(seen) == 1
    finally:
        source.close()


def test_provider_budget_block_does_not_retry_or_bypass(tmp_path, monkeypatch):
    """A true provider budget denial cannot be mistaken for a SQLite lock."""
    import pytest
    from bcc.data_cache import CacheBusy, PublicCache
    from bcc.market import DataUnavailable, PublicData

    shared = PublicCache(tmp_path / "budget.sqlite")
    source = PublicData()
    source.shared = shared
    attempts = []
    try:
        def denied(*args, **kwargs):
            attempts.append(1)
            raise CacheBusy("Общий бюджет публичных запросов исчерпан; ожидание")
        monkeypatch.setattr(shared, "begin", denied)
        with pytest.raises(DataUnavailable, match="Общий бюджет"):
            source.get(
                "https://api.bybit.com/v5/market/kline",
                {"symbol": "BTCUSDT"}, ttl=5,
            )
        assert len(attempts) == 1
    finally:
        source.close()


def test_sustained_sqlite_writer_lock_reports_and_allows_next_tick(tmp_path):
    """A PAPER worker cannot wait 13s for a 6s diagnostic/recovery cycle.

    A database writer may wait for the worker to record a diagnostic before
    releasing the lock. A bounded CacheContention must return without making
    any unreserved HTTP request. A later tick may then retry normally.
    """
    import sqlite3
    import pytest
    from bcc.data_cache import PublicCache
    from bcc.market import DataUnavailable, PublicData

    db_path = tmp_path / "locked.sqlite"
    shared = PublicCache(db_path)
    assert shared.denied_status("api.bybit.com") is None
    writer = sqlite3.connect(db_path, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    source = PublicData()
    source.shared = shared
    called = []

    class Response:
        status_code = 200
        headers = {}
        def raise_for_status(self):
            pass
        def json(self):
            return {"ok": True}

    class Session:
        def get(self, *args, **kwargs):
            called.append(True)
            return Response()
        def close(self):
            pass

    source._thread.session = Session()
    try:
        start = time.monotonic()
        with pytest.raises(DataUnavailable, match="database is locked"):
            source.get("https://api.bybit.com/v5/market/kline",
                       {"symbol": "BTCUSDT"}, ttl=5)
        assert time.monotonic() - start < 5.5
        assert called == []
        writer.rollback()
        assert source.get("https://api.bybit.com/v5/market/kline",
                          {"symbol": "BTCUSDT"}, ttl=5) == {"ok": True}
        assert called == [True]
    finally:
        if writer.in_transaction:
            writer.rollback()
        writer.close()
        source.close()
