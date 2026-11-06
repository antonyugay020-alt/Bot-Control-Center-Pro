"""Controller decisions for an owned launcher and a separately verified worker."""

from types import SimpleNamespace

import pytest
from test_core import config

from bcc.lifecycle import Lifecycle
from bcc.storage import BotBusy, Repository, atomic_json


def owned_launcher(tmp_path):
    repo = Repository(tmp_path)
    folder = repo.create(config())
    life = Lifecycle(repo)
    life.children[str(folder)] = SimpleNamespace(pid=100, poll=lambda: None)
    life.tokens[str(folder)] = "issued-token"
    snapshot = dict(pid=200, token="issued-token", created=1, heartbeat=1)
    atomic_json(folder / "process.json", snapshot)
    return folder, life, snapshot


def test_separate_worker_pid_requires_independent_verification_and_issued_token(
    tmp_path, monkeypatch
):
    folder, life, snapshot = owned_launcher(tmp_path)
    monkeypatch.setattr(life, "process", lambda path: None)
    assert life.active_snapshot(folder) is None
    assert life.status(folder) == "Запуск"
    monkeypatch.setattr(life, "process", lambda path: SimpleNamespace(pid=200))
    assert life.active_snapshot(folder) == snapshot
    assert life.status(folder) == "Работает"
    life.finish(folder)
    assert (folder / "finish.request").exists()
    snapshot["token"] = "another-launch-token"
    atomic_json(folder / "process.json", snapshot)
    assert life.active_snapshot(folder) is None


def test_owned_direct_pid_still_works_without_external_pid_namespace_access(
    tmp_path, monkeypatch
):
    folder, life, snapshot = owned_launcher(tmp_path)
    snapshot["pid"] = 100
    atomic_json(folder / "process.json", snapshot)
    monkeypatch.setattr(life, "process", lambda path: None)
    assert life.active_snapshot(folder) == snapshot


@pytest.mark.parametrize("confirmed", [False, True])
def test_startup_force_checks_launcher_before_stopping_its_tree(
    tmp_path, monkeypatch, confirmed
):
    folder, life, _ = owned_launcher(tmp_path)
    killed = []
    child = SimpleNamespace(
        pid=100, poll=lambda: None, kill=lambda: killed.append("launcher")
    )
    proc = SimpleNamespace(
        cmdline=lambda: [
            "python.exe",
            "--folder",
            str(folder),
            "--token",
            "issued-token" if confirmed else "unrelated",
        ],
        children=lambda recursive: [
            SimpleNamespace(kill=lambda: killed.append("worker"))
        ],
    )
    monkeypatch.setattr("bcc.lifecycle.psutil.Process", lambda pid: proc)
    if confirmed:
        life.kill_owned_launcher(child, folder)
        assert killed == ["worker", "launcher"]
    else:
        with pytest.raises(BotBusy):
            life.kill_owned_launcher(child, folder)
        assert killed == []


def test_different_pid_fallback_retains_creation_time_check(tmp_path, monkeypatch):
    folder, life, _ = owned_launcher(tmp_path)
    proc = SimpleNamespace(create_time=lambda: 99)
    monkeypatch.setattr("bcc.lifecycle.psutil.Process", lambda pid: proc)
    assert life.process(folder) is None
    assert life.active_snapshot(folder) is None


@pytest.mark.parametrize("confirmed", [False, True])
def test_force_stop_verifies_launcher_and_kills_descendants_on_all_platforms(
    tmp_path, monkeypatch, confirmed
):
    folder, life, _ = owned_launcher(tmp_path)
    terminated = []
    child = SimpleNamespace(pid=100, poll=lambda: None,
                            kill=lambda: terminated.append("launcher"))
    life.children[str(folder)] = child
    life.tokens[str(folder)] = "issued-token"
    monkeypatch.setattr(life, "process", lambda path: None)
    proc = SimpleNamespace(
        cmdline=lambda: ["python", "--folder", str(folder), "--token",
                         "issued-token" if confirmed else "other-token"],
        children=lambda recursive: [SimpleNamespace(
            kill=lambda: terminated.append("worker"))],
    )
    monkeypatch.setattr("bcc.lifecycle.psutil.Process", lambda pid: proc)
    if confirmed:
        life.stop(folder, force=True)
        assert terminated == ["worker", "launcher"]
    else:
        with pytest.raises(BotBusy):
            life.stop(folder, force=True)
        assert terminated == []
