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


def test_schema_wait_is_scoped_and_trading_cache_operations_remain_bounded(tmp_path):
    from bcc.data_cache import PublicCache
    cache = PublicCache(tmp_path / "lock-policy.sqlite")
    try:
        with cache.db(schema=True) as db:
            assert db.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000
        with cache.db() as db:
            assert db.execute("PRAGMA busy_timeout").fetchone()[0] <= 500
    finally:
        cache.close()
