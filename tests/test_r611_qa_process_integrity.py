"""Regression coverage for R6.11 evidence correctness and subprocess cleanup."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from tools import verify_windows, windows_check
from tools.process_guard import run_logged_command


def test_windows_steps_have_single_gui_and_stop_on_error(tmp_path, monkeypatch):
    steps = []
    monkeypatch.setattr(verify_windows, "is_native_windows", lambda: True)
    monkeypatch.setattr(verify_windows, "ROOT", tmp_path)
    monkeypatch.setattr(verify_windows, "source_fingerprint", lambda: "f" * 64)
    monkeypatch.setattr(verify_windows, "atomic_json", lambda path, report: path.write_text(json.dumps(report), encoding="utf-8"))
    from tools import verify_package
    monkeypatch.setattr(verify_package, "verify", lambda root: {"status": "PASS"})
    def fake_step(name, args, out, timeout):
        steps.append(name)
        return {"step": name, "status": "FAIL", "exit_code": 1}
    monkeypatch.setattr(verify_windows, "run_step", fake_step)
    monkeypatch.setattr(sys, "argv", ["verify_windows", "--output", str(tmp_path / "test-output")])
    assert verify_windows.main() == 1
    assert steps == ["collection"]
    assert json.loads((tmp_path / "test-output" / "windows_report.json").read_text(encoding="utf-8"))["status"] == "FAIL"


def test_windows_qa_refuses_stale_evidence_directory(tmp_path, monkeypatch):
    out = tmp_path / "existing"
    out.mkdir()
    (out / "tests.xml").write_text("stale evidence", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["verify_windows", "--output", str(out)])
    assert verify_windows.main() == 1
    assert (out / "tests.xml").read_text(encoding="utf-8") == "stale evidence"


def test_checker_logged_commands_time_out_without_claiming_pass(tmp_path):
    out = tmp_path / "report"
    out.mkdir()
    result = windows_check.run_step("sleeping", [sys.executable, "-c", "import time;time.sleep(3)"], out, timeout=0.1)
    assert result["exit_code"] is None and result["error"] == "Timeout"


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups are tested here; Windows taskkill requires native test")
def test_timeout_kills_descendant_process_group(tmp_path):
    pid_file = tmp_path / "child.pid"
    code = ("import subprocess,sys,time; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            f"open({str(pid_file)!r},'w').write(str(p.pid)); time.sleep(60)")
    log = tmp_path / "out.log"
    with log.open("w", encoding="utf-8") as stream:
        with pytest.raises(subprocess.TimeoutExpired):
            run_logged_command([sys.executable, "-c", code], cwd=tmp_path, stdout=stream, timeout=2.0)
    assert pid_file.exists(), log.read_text(encoding="utf-8")
    child = int(pid_file.read_text(encoding="utf-8"))
    # Linux may briefly retain a zombie child until its parent is reaped.
    from bcc import process_os
    assert (not process_os.pid_exists(child)
            or process_os.Process(child).status() == process_os.STATUS_ZOMBIE)


def test_successful_steps_run_gui_once(tmp_path, monkeypatch):
    monkeypatch.setattr(verify_windows, "is_native_windows", lambda: True)
    monkeypatch.setattr(verify_windows, "ROOT", tmp_path)
    monkeypatch.setattr(verify_windows, "source_fingerprint", lambda: "f" * 64)
    from tools import verify_package
    monkeypatch.setattr(verify_package, "verify", lambda root: {"status": "PASS"})
    sequence = []
    def fake_step(name, args, out, timeout):
        sequence.append(name)
        return {"step": name, "status": "PASS", "exit_code": 0}
    monkeypatch.setattr(verify_windows, "run_step", fake_step)
    monkeypatch.setattr(sys, "argv", ["verify_windows", "--output", str(tmp_path / "verify")])
    # Missing evidence must fail even if every command returned 0.
    assert verify_windows.main() == 1
    assert sequence == ["collection", "tests", "gui", "public_sources"]


def test_windows_taskkill_nonzero_falls_back_to_killing_parent(tmp_path, monkeypatch):
    # Test the Windows timeout path without launching a Windows process on Linux.
    from types import SimpleNamespace
    from tools import process_guard

    class Process:
        pid = 123456
        killed = False
        calls = 0

        def wait(self, *, timeout):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("fake", timeout)
            return -9

        def poll(self):
            return None if not self.killed else -9

        def kill(self):
            self.killed = True

    process = Process()
    commands = []
    monkeypatch.setattr(process_guard, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(process_guard.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False)
    monkeypatch.setattr(process_guard.subprocess, "Popen", lambda *args, **kw: process)

    def taskkill(args, **kw):
        commands.append(args)
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(process_guard.subprocess, "run", taskkill)
    with (tmp_path / "windows-fallback.log").open("w", encoding="utf-8") as stream:
        with pytest.raises(subprocess.TimeoutExpired):
            process_guard.run_logged_command(["cmd", "/c", "exit"], cwd=tmp_path,
                                             stdout=stream, timeout=1)
    assert len(commands) == 1 and commands[0][0] == "taskkill.exe"
    assert process.killed


def test_failed_windows_taskkill_still_cleans_observed_descendants(tmp_path, monkeypatch):
    """A failed taskkill must not orphan a child after its launcher exits."""
    from types import SimpleNamespace
    from tools import process_guard

    class Child:
        def __init__(self):
            self.killed = False
        def kill(self):
            self.killed = True

    child = Child()

    class Parent:
        pid = 54321
        attempts = 0
        def wait(self, *, timeout):
            self.attempts += 1
            if self.attempts == 1:
                raise subprocess.TimeoutExpired("fake", timeout)
            return 1
        def poll(self):
            return 1  # Launcher already exited; its child is still alive.
        def kill(self):
            raise AssertionError("exited parent must not be killed again")

    monkeypatch.setattr(process_guard, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(process_guard.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False)
    monkeypatch.setattr(process_guard.subprocess, "Popen", lambda *a, **kw: Parent())
    monkeypatch.setattr(process_guard.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=1))
    monkeypatch.setattr(process_guard.psutil, "Process", lambda pid: SimpleNamespace(children=lambda recursive: [child]))
    calls = []
    def wait_procs(children, timeout):
        calls.append((children, timeout))
        return ([], [child]) if len(calls) == 1 else ([child], [])
    monkeypatch.setattr(process_guard.psutil, "wait_procs", wait_procs)
    with (tmp_path / "windows.log").open("w", encoding="utf-8") as stream:
        with pytest.raises(subprocess.TimeoutExpired):
            run_logged_command(["fake.exe"], cwd=tmp_path, stdout=stream, timeout=1)
    assert child.killed
    assert len(calls) == 2


def test_verify_windows_reports_missing_source_before_fingerprinting(tmp_path, monkeypatch):
    from tools import verify_windows
    monkeypatch.setattr(verify_windows, "ROOT", tmp_path)
    monkeypatch.setattr(verify_windows, "is_native_windows", lambda: True)
    monkeypatch.setattr(sys, "argv", ["verify_windows", "--output", str(tmp_path / "results")])
    # Neither START.bat nor SHA256_MANIFEST.json exists. We must get a
    # structured failure report, not a traceback before the report is created.
    assert verify_windows.main() == 1
    data = json.loads((tmp_path / "results/windows_report.json").read_text(encoding="utf-8"))
    assert data["status"] == "FAIL"
    assert data["source_integrity"]["status"] == "FAIL"
    assert data["source_sha256"] is None


def test_verify_windows_run_step_reports_missing_executable(tmp_path, monkeypatch):
    from tools import verify_windows
    from tools import process_guard
    def missing(*args, **kwargs):
        raise FileNotFoundError("test interpreter missing")
    monkeypatch.setattr(process_guard, "run_logged_command", missing)
    monkeypatch.setattr(verify_windows, "run_logged_command", missing)
    result = verify_windows.run_step("tests", ["-m", "pytest"], tmp_path, 10)
    assert result["status"] == "FAIL"
    assert "test interpreter missing" in result["error"]


def test_process_cleanup_timeout_is_not_masked_by_psutil_error(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from tools import process_guard

    class Parent:
        pid = 54320
        calls = 0
        def wait(self, *, timeout):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("fake", timeout)
            return 1
        def poll(self):
            return 1
    child = SimpleNamespace(kill=lambda: None)
    monkeypatch.setattr(process_guard, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(process_guard.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False)
    monkeypatch.setattr(process_guard.subprocess, "Popen", lambda *a, **kw: Parent())
    monkeypatch.setattr(process_guard.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=1))
    monkeypatch.setattr(process_guard.psutil, "Process", lambda pid: SimpleNamespace(children=lambda recursive: [child]))
    monkeypatch.setattr(process_guard.psutil, "wait_procs", lambda *a, **kw: (_ for _ in ()).throw(process_guard.psutil.AccessDenied(54321)))
    with (tmp_path / "failed.log").open("w", encoding="utf-8") as stream:
        with pytest.raises(subprocess.TimeoutExpired):
            run_logged_command(["broken.exe"], cwd=tmp_path, stdout=stream, timeout=1)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group orphan check")
def test_successful_exit_with_live_child_cannot_pass(tmp_path):
    """A process that exits 0 while leaving a worker previously got PASS."""
    pid_file = tmp_path / "orphan.pid"
    code = (
        "import subprocess,sys; "
        "p=subprocess.Popen([sys.executable,'-c',"
        "'import time;time.sleep(45)'],"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
        f"open({str(pid_file)!r},'w').write(str(p.pid))"
    )
    log = tmp_path / "qa.log"
    with log.open("w", encoding="utf-8") as stream:
        with pytest.raises(OSError, match="left live child processes"):
            run_logged_command([sys.executable, "-c", code],
                               cwd=tmp_path, stdout=stream, timeout=8)
    from bcc import process_os as psutil
    assert pid_file.exists()
    pid = int(pid_file.read_text(encoding="utf-8"))
    assert (not psutil.pid_exists(pid)
            or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group orphan check")
def test_successful_exit_without_children_is_still_allowed(tmp_path):
    with (tmp_path / "qa.log").open("w", encoding="utf-8") as stream:
        assert run_logged_command([sys.executable, "-c", "print('ok')"],
                                  cwd=tmp_path, stdout=stream, timeout=8) == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX demonstration of detached child")
def test_successful_exit_with_detached_paper_child_cannot_pass(tmp_path):
    """Real BCC lifecycle launches a worker with start_new_session=True."""
    pid_file = tmp_path / "detached.pid"
    code = (
        "import subprocess,sys,time; "
        "p=subprocess.Popen([sys.executable,'-c',"
        "'import time;time.sleep(40)'],"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,"
        "start_new_session=True); "
        f"open({str(pid_file)!r},'w').write(str(p.pid)); "
        "time.sleep(0.3)"
    )
    with (tmp_path / "qa.log").open("w", encoding="utf-8") as stream:
        with pytest.raises(OSError, match="left live child processes"):
            run_logged_command([sys.executable, "-c", code],
                               cwd=tmp_path, stdout=stream, timeout=8)
    from bcc import process_os as psutil
    assert pid_file.exists()
    pid = int(pid_file.read_text(encoding="utf-8"))
    assert (not psutil.pid_exists(pid)
            or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE)
