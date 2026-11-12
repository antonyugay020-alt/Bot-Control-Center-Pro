"""Fleet transfer is one admission transaction, not a series of per-bot copies."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from threading import Event
import zipfile

import pytest

from bcc.storage import Repository, Store
from bcc.supervisor import Supervisor
from test_core import config


def _two_accounts(tmp_path):
    repo = Repository(tmp_path / "app")
    first = repo.create(config())
    second = repo.create(config())
    for folder in (first, second):
        Store(folder, 500)
    return Supervisor(repo), first, second


def test_fleet_export_holds_earlier_owner_until_last_sqlite_snapshot(
    tmp_path, monkeypatch
):
    supervisor, first, second = _two_accounts(tmp_path)
    entered = Event()
    release = Event()
    submitted = Event()
    backup = Store.backup

    def delayed_second_snapshot(self, destination):
        if self.folder == second:
            entered.set()
            if not release.wait(15):
                raise TimeoutError("test did not release second PAPER snapshot")
        return backup(self, destination)

    monkeypatch.setattr(Store, "backup", delayed_second_snapshot)
    archive = tmp_path / "fleet.zip"

    def request_start():
        submitted.set()
        return supervisor.submit(first, running=True)

    with ThreadPoolExecutor(max_workers=2) as workers:
        exported = workers.submit(
            supervisor.export_bots, [first, second], archive, history=True
        )
        try:
            assert entered.wait(10), "second account backup was never reached"
            start = workers.submit(request_start)
            assert submitted.wait(5), "START was never attempted"
            # Per-bot export gates release the first owner too early.
            # The new fleet admission fence must keep it blocked here.
            with pytest.raises(FutureTimeout):
                start.result(timeout=0.25)
        finally:
            release.set()
        exported.result(timeout=20)
        assert start.result(timeout=20).desired == "RUNNING"

    with zipfile.ZipFile(archive) as z:
        assert {"bot_0000/paper.sqlite", "bot_0001/paper.sqlite"} <= set(z.namelist())


def test_fleet_export_error_releases_all_admission_gates_without_overwrite(
    tmp_path, monkeypatch
):
    supervisor, first, second = _two_accounts(tmp_path)
    target = tmp_path / "existing.zip"
    target.write_bytes(b"preserve prior export")
    original_backup = Store.backup

    def failing_second_backup(self, destination):
        if self.folder == second:
            raise OSError("injected second account backup failure")
        return original_backup(self, destination)

    monkeypatch.setattr(Store, "backup", failing_second_backup)
    with pytest.raises(OSError, match="injected"):
        supervisor.export_bots([first, second], target, history=True)
    assert target.read_bytes() == b"preserve prior export"
    assert not list(tmp_path.glob(".bcc-export-*"))
    # Failure must not leave an owner in a permanently locked state.
    assert supervisor.submit(first, running=True).desired == "RUNNING"


def test_fleet_export_rejects_duplicate_owner_without_overwriting_archive(tmp_path):
    supervisor, first, _ = _two_accounts(tmp_path)
    target = tmp_path / "existing.zip"
    target.write_bytes(b"keep this")
    with pytest.raises(ValueError, match="Повторяющийся бот"):
        supervisor.export_bots([first, first], target, history=True)
    assert target.read_bytes() == b"keep this"
