"""Test-only teardown for separately launched, independently running market hubs.

A real worker's Market Data Hub intentionally survives its parent GUI and may
idle for up to 90 seconds. Test cases using real Lifecycle.start must clean up
ONLY their own verified subprocesses before the QA command can count as PASS.
No production process or user workspace may be touched by this fixture.
"""
import json

from bcc import process_os as psutil
import pytest

from bcc.hub_supervisor import _matches


def cleanup_owned_test_hubs(root):
    """Stop live hubs proven to belong to this pytest temporary directory.

    Match process PID, OS creation time, exact --cache path and module before
    sending any signal. A recycled PID or external user hub is never killed.
    """
    for record_path in root.rglob("market_hub_process.json"):
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        cache = (record_path.parent / "market_cache.sqlite").resolve()
        if not _matches(record, cache):
            continue
        pid = record["pid"]
        try:
            proc = psutil.Process(pid)
            # Reconfirm immediately before termination; PID reuse is possible.
            if not _matches(record, cache):
                continue
            proc.terminate()
            try:
                proc.wait(timeout=4)
            except psutil.TimeoutExpired:
                if _matches(record, cache):
                    proc.kill()
                    proc.wait(timeout=4)
                else:
                    # Do not kill an unverified replacement process.
                    continue
        except psutil.NoSuchProcess:
            continue
        # AccessDenied or a second TimeoutExpired intentionally fails the
        # test and blocks release instead of hiding a lingering producer.


@pytest.fixture(autouse=True)
def cleanup_test_services(tmp_path, request):
    yield
    if getattr(request.node, '_bcc_failed', False):
        _capture_failed_hub_logs(tmp_path, request.node.name)
    cleanup_owned_test_hubs(tmp_path)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    result = outcome.get_result()
    if result.when == 'call':
        item._bcc_failed = result.failed


def _capture_failed_hub_logs(root, test_name):
    """Attach only logs of this isolated pytest workspace, never user bots."""
    import os
    import re
    import shutil
    from pathlib import Path
    output = os.environ.get('BCC_TEST_ARTIFACTS')
    if not output:
        return
    dest = Path(output) / 'hub_failures'
    safe = re.sub(r'[^a-zA-Z0-9._-]', '_', test_name)[-90:]
    for index, source in enumerate(root.rglob('market_hub.log')):
        if not source.is_file() or source.is_symlink():
            continue
        try:
            dest.mkdir(parents=True, exist_ok=True)
            with source.open('rb') as stream:
                stream.seek(0, 2)
                stream.seek(max(0, stream.tell() - 8192))
                tail = stream.read()
            (dest / f'{safe}_{index}.log').write_bytes(tail)
        except OSError:
            pass  # The test's actual failure remains visible in JUnit.
