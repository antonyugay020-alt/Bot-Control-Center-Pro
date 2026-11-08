"""OS-backed identity, namespace isolation and exact-generation signalling."""
import os
from pathlib import Path
import signal
import subprocess
import sys
from types import SimpleNamespace

# import pytest (fixed line)n@pytest.mark.skipif(os.name == "nt", reason="hard link not supported on Windows")


from bcc import process_os


def launch_observed_probe(tmp_path, body):
    import time
    receipt = tmp_path / 'actual-interpreter.pid'
    release = Path(str(receipt) + '.release')
    code = ("import os,sys,time\n"
            "with open(sys.argv[1]+'.tmp','w') as stream:\n"
            " stream.write(str(os.getpid()))\n"
            "os.replace(sys.argv[1]+'.tmp',sys.argv[1])\n"
            "while not os.path.exists(sys.argv[1]+'.release'):\n"
            " time.sleep(.01)\n" + body)
    launcher = subprocess.Popen([sys.executable, '-c', code, str(receipt)])
    try:
        deadline = time.monotonic() + 5
        while not receipt.exists() and launcher.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert receipt.exists(), 'interpreter failed to publish its own PID'
        identity = process_os.Process(int(receipt.read_text(encoding="utf-8")))
        assert str(receipt) in identity.cmdline()
        assert (identity.pid == launcher.pid
                or any(p.pid == launcher.pid for p in identity.parents()))
        return launcher, identity, release
    except BaseException:
        if launcher.poll() is None:
            launcher.kill()
        launcher.wait(timeout=5)
        raise


def cleanup_probe(launcher, identity):
    if identity.is_running() and identity.status() != process_os.STATUS_ZOMBIE:
        identity.kill()
        identity.wait(timeout=5)
    if launcher.poll() is None:
        launcher.kill()
    launcher.wait(timeout=5)


def test_real_child_identity_ancestry_and_exit_status_are_independent_of_popen(tmp_path):
    launcher, identity, release = launch_observed_probe(tmp_path, 'raise SystemExit(7)')
    try:
        assert identity.create_time() > 0
        assert any(parent.pid == os.getpid() for parent in identity.parents())
        assert any(candidate.pid == identity.pid
                   for candidate in process_os.Process().children(recursive=True))
        release.write_text("go", encoding="utf-8")
        if process_os._NAMESPACED:
            # WNOWAIT preserves the launcher owner's exit status.
            assert identity.wait(timeout=5) == 7
        assert launcher.wait(timeout=5) == 7
        assert not identity.is_running() or identity.status() == process_os.STATUS_ZOMBIE
    finally:
        cleanup_probe(launcher, identity)


def test_real_signal_targets_the_owned_child_and_preserves_popen_exit_code(tmp_path):
    launcher, identity, _ = launch_observed_probe(tmp_path, 'time.sleep(30)')
    try:
        identity.terminate()
        # TerminateProcess on Windows is asynchronous. A python.exe/py.exe
        # redirector can exit before the *observed* interpreter finishes;
        # waiting only on Popen is not proof that the owned PID is gone.
        # Linux namespace wait uses WNOWAIT so Popen still owns reaping.
        if process_os._NAMESPACED or os.name == "nt":
            identity.wait(timeout=5)
        assert launcher.wait(timeout=5) != 0
        assert not identity.is_running() or identity.status() == process_os.STATUS_ZOMBIE
    finally:
        cleanup_probe(launcher, identity)


def fake_procfs(tmp_path, monkeypatch):
    root = tmp_path / 'proc'
    namespace = root / 'self/ns/pid'
    namespace.parent.mkdir(parents=True)
    namespace.write_text('owned namespace', encoding="utf-8")
    monkeypatch.setattr(process_os, '_PROC', root)
    monkeypatch.setattr(process_os, '_CACHE', {})
    monkeypatch.setattr(process_os, '_probe_local_pid', lambda pid: None)
    return root, namespace


def add_entry(root, host, local, namespace=None):
    path = root / str(host)
    (path / 'ns').mkdir(parents=True)
    (path / 'status').write_text(f'State:\tS (sleeping)\nNSpid:\t{host}\t{local}\n', encoding="utf-8")
    if namespace is None:
        (path / 'ns/pid').write_text('foreign namespace', encoding="utf-8")
    else:
        os.link(namespace, path / 'ns/pid')


def test_numeric_host_pid_collision_does_not_select_foreign_process(tmp_path, monkeypatch):
    root, namespace = fake_procfs(tmp_path, monkeypatch)
    add_entry(root, host=42, local=42)
    add_entry(root, host=100, local=42, namespace=namespace)
    # Even a stale cache pointing at the same numeric host PID is untrusted.
    process_os._CACHE[42] = 42
    assert process_os._resolve(42) == (100, namespace.stat().st_ino)


def test_foreign_namespace_is_not_authority_even_when_nspid_matches(tmp_path, monkeypatch):
    root, _ = fake_procfs(tmp_path, monkeypatch)
    add_entry(root, host=100, local=42)
    with pytest.raises(process_os.AccessDenied, match='cannot be verified'):
        process_os._resolve(42)


def test_pid_reuse_invalidates_cached_mapping(tmp_path, monkeypatch):
    root, namespace = fake_procfs(tmp_path, monkeypatch)
    add_entry(root, host=100, local=42, namespace=namespace)
    assert process_os._resolve(42)[0] == 100
    (root / '100/status').write_text('NSpid:\t100\t43\n', encoding="utf-8")
    add_entry(root, host=101, local=42, namespace=namespace)
    assert process_os._resolve(42)[0] == 101


def test_pidfd_pins_generation_and_never_signals_host_inspection_pid(monkeypatch):
    calls = []
    process = process_os._NamespaceProcess.__new__(process_os._NamespaceProcess)
    process.pid, process._host_pid = 42, 100
    process._assert_identity = lambda: calls.append('verify')
    monkeypatch.setattr(process_os, 'os', SimpleNamespace(
        pidfd_open=lambda pid, flags: calls.append(('open', pid)) or 77,
        close=lambda fd: calls.append(('close', fd))))
    monkeypatch.setattr(process_os, 'signal', SimpleNamespace(
        pidfd_send_signal=lambda fd, sig: calls.append(('signal', fd, sig))))
    process.send_signal(signal.SIGTERM)
    assert calls == ['verify', ('open', 42), 'verify',
                     ('signal', 77, signal.SIGTERM), ('close', 77)]


def test_generation_change_after_pidfd_open_delivers_no_signal(monkeypatch):
    calls = []
    process = process_os._NamespaceProcess.__new__(process_os._NamespaceProcess)
    process.pid = 42
    def verify():
        calls.append('verify')
        if len(calls) > 1:
            raise process_os.NoSuchProcess(42)
    process._assert_identity = verify
    monkeypatch.setattr(process_os, 'os', SimpleNamespace(
        pidfd_open=lambda pid, flags: 77, close=lambda fd: calls.append('closed')))
    monkeypatch.setattr(process_os, 'signal', SimpleNamespace(
        pidfd_send_signal=lambda *args: pytest.fail('signal sent to a reused PID')))
    with pytest.raises(process_os.NoSuchProcess):
        process.send_signal(signal.SIGTERM)
    assert calls[-1] == 'closed'


def test_native_backend_delegates_to_os_process_library(monkeypatch):
    expected = object()
    monkeypatch.setattr(process_os, '_NAMESPACED', False)
    monkeypatch.setattr(process_os._psutil, 'Process', lambda pid: expected if pid == 123 else None)
    assert process_os.Process(123) is expected
