"""The headless PASS gate must prove it does not orphan real Hub services."""
import json
import os

from bcc import process_os as psutil

from bcc.hub_supervisor import ensure_hub
from conftest import cleanup_owned_test_hubs


def test_cleanup_terminates_only_a_verified_test_hub(tmp_path):
    root = tmp_path / "isolation" / "MY_BOTS"
    root.mkdir(parents=True)
    pid = ensure_hub(root)
    assert psutil.pid_exists(pid)
    cleanup_owned_test_hubs(tmp_path)
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


def test_cleanup_ignores_unrelated_or_recycled_pid(tmp_path):
    folder = tmp_path / "isolation"
    folder.mkdir()
    record = {"pid": os.getpid(), "created": psutil.Process().create_time()}
    (folder / "market_hub_process.json").write_text(json.dumps(record), encoding="utf-8")
    cleanup_owned_test_hubs(tmp_path)
    assert psutil.Process().is_running()
