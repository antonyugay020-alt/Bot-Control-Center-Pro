import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from bcc import process_os as psutil

from bcc.hub_supervisor import ensure_hub


def test_concurrent_start_reuse_and_crash_recovery(tmp_path):
    with ThreadPoolExecutor(max_workers=10) as pool:
        pids = list(pool.map(lambda _: ensure_hub(tmp_path), range(15)))
    assert len(set(pids)) == 1
    first = psutil.Process(pids[0])
    try:
        assert ensure_hub(tmp_path) == pids[0]
        first.kill()
        first.wait(timeout=5)
        recovered = ensure_hub(tmp_path)
        assert recovered != pids[0]
        assert ensure_hub(tmp_path) == recovered
    finally:
        record = tmp_path / 'market_hub_process.json'
        if record.exists():
            pid = json.loads(record.read_text(encoding="utf-8"))['pid']
            try:
                proc = psutil.Process(pid)
                proc.terminate()
                proc.wait(timeout=5)
            except psutil.NoSuchProcess:
                pass
