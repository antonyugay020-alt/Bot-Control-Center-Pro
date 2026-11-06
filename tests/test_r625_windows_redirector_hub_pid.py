"""Windows venv launcher PID must not be confused with actual Hub child PID."""
import json
import subprocess
import sys
import time
from pathlib import Path

from bcc import process_os as psutil

from bcc.hub_supervisor import _verified_redirected_hub_pid, ensure_hub, _matches


def test_redirected_hub_identifies_actual_service_and_persists_real_pid(tmp_path, monkeypatch):
    import bcc.hub_supervisor as supervisor

    actual_launches = []
    launchers = []
    verified_service = None

    def redirector(args, **kwargs):
        # The Popen PID is not necessarily the Python interpreter PID on
        # Windows. A venv launcher can delegate to a second executable.
        # Start a genuine Hub and independently return a fake launcher so
        # ensure_hub MUST authenticate the child-written receipt instead.
        worker_launch = subprocess.Popen(args, **kwargs)
        actual_launches.append(worker_launch)
        launcher = subprocess.Popen([sys.executable, '-c', 'pass'], **kwargs)
        launchers.append(launcher)
        return launcher

    monkeypatch.setattr(supervisor, '_spawn_hub', redirector)
    try:
        pid = ensure_hub(tmp_path, timeout=8)
        record = json.loads((tmp_path / 'market_hub_process.json').read_text(encoding="utf-8"))
        assert record['pid'] == pid
        assert _matches(record, tmp_path / 'market_cache.sqlite')
        assert pid != launchers[0].pid

        # Check the executing service, not Popen's potentially redirected PID.
        # In Windows venvs actual_launches[0].pid can itself be a redirector.
        service = psutil.Process(pid)
        verified_service = (pid, service.create_time())
        assert service.is_running()
        cmd = service.cmdline()
        assert 'tools.market_hub_service' in cmd
        assert '--cache' in cmd
        assert Path(cmd[cmd.index('--cache') + 1]).resolve() == (tmp_path / 'market_cache.sqlite').resolve()
        assert abs(record['created'] - service.create_time()) < .05
        log = (tmp_path / 'market_hub.log').read_text(encoding='utf-8')
        assert f'Market Hub starting: pid={pid} ' in log
        assert 'Market Hub leader lease acquired' in log
    finally:
        # First terminate the authenticated real service. Killing only its
        # Windows .venv Popen redirector can orphan the market producer.
        if verified_service is not None:
            service_pid, created = verified_service
            try:
                service = psutil.Process(service_pid)
                if abs(service.create_time() - created) < .05:
                    cmd = service.cmdline()
                    if 'tools.market_hub_service' in cmd:
                        service.terminate()
                        try:
                            service.wait(timeout=5)
                        except psutil.TimeoutExpired:
                            if abs(service.create_time() - created) < .05:
                                service.kill()
                                service.wait(timeout=5)
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                pass
        for process in actual_launches + launchers:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def test_redirected_hub_rejects_wrong_token_or_incorrect_paths(tmp_path):
    cache = (tmp_path / 'cache.sqlite').resolve()
    ready = (tmp_path / 'ready.json').resolve()
    token = 'a' * 32
    # This ordinary process is not the authorized Hub even if PID is known.
    proc = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(5)'])
    try:
        assert _verified_redirected_hub_pid(proc.pid, cache, ready, token, time.time() - 1) is None
    finally:
        proc.terminate()
        proc.wait(timeout=5)
