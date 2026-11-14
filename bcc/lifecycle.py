"""OS-backed lifecycle. A PID alone never authorizes terminating a process."""

import math
import os
import subprocess
import sys
import uuid
from pathlib import Path

from . import process_os as psutil

from .hub_supervisor import ensure_hub
from .control import stop_requested
from .runtime_paths import python_environment
from .launch_claim import (
    check_launch_lease, claim_pending, claim_path, issue_launch,
    read_claim, register_launcher,
)
from .storage import BotBusy, BotLock, atomic_json, load_json, guard_paper_recovery


class Lifecycle:
    def __init__(self, repo):
        self.repo = repo
        self.children = {}
        self.tokens = {}

    def process(self, folder):
        folder = self.repo.resolve(folder)
        try:
            data = load_json(folder / "process.json")
            if not isinstance(data, dict):
                return None
            pid, created, token = (data.get("pid"), data.get("created"),
                                   data.get("token"))
            if (type(pid) is not int or pid <= 0 or
                    type(created) not in (int, float) or
                    not math.isfinite(created) or created <= 0 or
                    type(token) is not str or not token):
                return None
            p = psutil.Process(pid)
            actual_created = p.create_time()
            if (type(actual_created) not in (int, float) or
                    not math.isfinite(actual_created) or
                    abs(actual_created - created) > 0.01):
                return None
            args = p.cmdline()
            if (not isinstance(args, (list, tuple)) or
                    args.count("--token") != 1 or
                    args.count("--folder") != 1):
                return None
            token_index = args.index("--token") + 1
            folder_index = args.index("--folder") + 1
            if (token_index >= len(args) or folder_index >= len(args) or
                    args[token_index] != token or
                    not isinstance(args[folder_index], str) or
                    Path(args[folder_index]).resolve() != folder):
                return None
            if p.status() == psutil.STATUS_ZOMBIE:
                return None
            return p
        except (OSError, ValueError, KeyError, TypeError, IndexError,
                OverflowError, psutil.NoSuchProcess):
            return None
        except psutil.AccessDenied:
            raise BotBusy("Нельзя подтвердить процесс бота: нет доступа")

    def status(self, folder):
        folder = self.repo.resolve(folder)
        child = self.children.get(str(folder))
        if child is not None and child.poll() is None:
            if stop_requested(folder, self.tokens.get(str(folder))):
                return "Остановка"
            snapshot = self.active_snapshot(folder)
            if snapshot and (
                (folder / "finish.request").exists() or snapshot.get("draining")
            ):
                return "Завершает сделки"
            return "Работает" if snapshot else "Запуск"
        p = self.process(folder)
        if p:
            # process() verified PID, creation time, command line and token.
            # A separate file write can race with our subsequent read. Do not
            # crash the whole monitoring UI (or report a verified live process
            # as stopped) if process.json is replaced during this interval.
            try:
                snapshot = load_json(folder / "process.json")
            except (OSError, UnicodeError, ValueError, TypeError):
                return "Занят другим процессом"
            if (not isinstance(snapshot, dict) or
                    type(snapshot.get("token")) is not str or
                    not snapshot["token"] or
                    snapshot.get("pid") != p.pid or
                    type(snapshot.get("created")) not in (int, float) or
                    not math.isfinite(snapshot["created"]) or
                    abs(snapshot["created"] - p.create_time()) > 0.01):
                return "Занят другим процессом"
            if stop_requested(folder, snapshot["token"]):
                return "Остановка"
            if (folder / "finish.request").exists() or snapshot.get("draining"):
                return "Завершает сделки"
            return "Работает"
        try:
            with BotLock(folder):
                claim = read_claim(folder)
                if claim is not None and claim_pending(claim):
                    return "Запуск"
        except BotBusy:
            return "Занят другим процессом"
        return "Остановлен"

    def last_error(self, folder):
        """Persisted worker failure for display; never used as process identity."""
        folder = self.repo.resolve(folder)
        path = folder / "process.json"
        if not path.is_file():
            return ""
        try:
            process = load_json(path)
        except (OSError, ValueError, UnicodeError, TypeError):
            return "Повреждена запись процесса (process.json); проверьте журнал"
        if not isinstance(process, dict):
            return "Повреждена запись процесса (process.json); проверьте журнал"
        return (
            str(process.get("last_error") or "Причина не записана")
            if process.get("status") == "ERROR"
            else ""
        )

    def active_snapshot(self, folder):
        """An owned live child is identified by its launch token and startup PID.

        After reopening the shell, only independent OS verification is accepted.
        This also works when a container's /proc lists host rather than child PIDs.
        """
        folder = self.repo.resolve(folder)
        child = self.children.get(str(folder))
        try:
            snapshot = load_json(folder / "process.json")
            if child is not None and child.poll() is None:
                if snapshot["token"] != self.tokens.get(str(folder)):
                    return None
                if snapshot["pid"] == child.pid:
                    return snapshot
                # A Windows venv executable may be a redirector: Popen owns the
                # launcher while the actual worker has a different PID. Accept
                # it only after independent OS path/token/creation-time checks.
                return snapshot if self.process(folder) else None
            return snapshot if self.process(folder) else None
        except (OSError, ValueError, KeyError, TypeError, IndexError,
                OverflowError):
            return None

    def start(self, folder):
        folder = self.repo.resolve(folder)
        cfg, _ = self.repo.load(folder)
        # This precheck only improves the error text; the actual decision is
        # made under the OS lock together with the durable launch claim.
        if self.status(folder) != "Остановлен":
            raise BotBusy("Бот уже работает или запускается")
        token = uuid.uuid4().hex
        app_root = Path(__file__).resolve().parent.parent
        env = python_environment()
        env["PYTHONPATH"] = str(app_root)
        env["PYTHONIOENCODING"] = "utf-8"
        env["BCC_PUBLIC_CACHE"] = str(self.repo.root / "market_cache.sqlite")
        # Polymarket uses a separate market/settlement adapter; the public
        # Bybit hub must not launch for bots that never subscribe to it.
        if cfg["strategy"] != "POLYMARKET":
            # New non-Polymarket workers require the active public Bybit
            # WebSocket quote + executable depth producer, not optional
            # legacy REST polling. Never interrupt a live legacy Hub:
            # ensure_hub rejects a capability mismatch rather than restarting it.
            ensure_hub(self.repo.root, bybit_ws=True)
            env["BCC_MARKET_HUB_MODE"] = "client"
            env["BCC_BYBIT_WS_BOOKS"] = "1"
            env["BCC_BYBIT_WS_QUOTES"] = "1"
        with BotLock(folder):
            check_launch_lease(folder)
            guard_paper_recovery(folder)
            # Both UI and service publish durable desired state; OS process
            # creation is not permitted by a stale supervisor observation.
            control_revision = None
            control_db = self.repo.root / 'coordination.sqlite'
            if control_db.exists():
                from .coordination import Coordinator
                control = Coordinator(control_db)
                key = folder.relative_to(self.repo.bots).as_posix()
                row = control.get_registration(key)
                if row is None or row['desired'] != 'RUNNING':
                    raise BotBusy('Запуск отменён командой управляющей службы')
                control_revision = row['command_version']
            (folder / "stop.request").unlink(missing_ok=True)
            issue_launch(folder, token)
            try:
                args = [
                    sys.executable, "-m", "bcc.worker",
                    "--folder", str(folder), "--token", token,
                ]
                if control_revision is not None:
                    args.extend(['--control-revision', str(control_revision)])
                flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                with (folder / "console.log").open("ab") as log:
                    proc = subprocess.Popen(
                        args, cwd=app_root, env=env,
                        stdout=log, stderr=subprocess.STDOUT,
                        creationflags=flags, start_new_session=os.name != "nt",
                    )
                # The worker will wait for this same lock, then validate token.
                register_launcher(folder, token, proc.pid)
            except BaseException:
                # The worker cannot pass BotLock until we release it. Revoke
                # its launch authorization *under the lock* so even an
                # unkillable redirector's child fails closed on startup.
                claim = read_claim(folder)
                if claim is not None and claim.get("token") == token:
                    claim_path(folder).unlink(missing_ok=True)
                if "proc" in locals():
                    # Stop the launcher and any children which already exist.
                    # Do not hide the original registration failure.
                    try:
                        parent = psutil.Process(proc.pid)
                        descendants = parent.children(recursive=True)
                        for descendant in reversed(descendants):
                            try:
                                descendant.kill()
                            except psutil.NoSuchProcess:
                                pass
                        if proc.poll() is None:
                            proc.kill()
                        proc.wait(timeout=5)
                    except (OSError, psutil.Error, subprocess.TimeoutExpired):
                        pass
                raise
        self.children[str(folder)] = proc
        self.tokens[str(folder)] = token
        return proc.pid

    def revoke_pending_launch(self, folder):
        """Revoke an uncommitted startup without targeting an unverified PID.

        The launch token is authoritative only until worker verifies it while
        holding BotLock.  If that lock is held by the worker, reconciliation
        must retry once the worker publishes a verifiable identity or exits.
        """
        folder = self.repo.resolve(folder)
        try:
            with BotLock(folder):
                claim = read_claim(folder)
                if claim is None:
                    return False
                # An already acknowledged worker has removed the claim. Never
                # revoke a running worker based on the old launcher PID.
                claim_path(folder).unlink(missing_ok=True)
                return True
        except BotBusy:
            return False

    def stop(self, folder, force=False):
        folder = self.repo.resolve(folder)
        proc = self.process(folder)
        if proc is None:
            child = self.children.get(str(folder))
            if child is None or child.poll() is not None:
                raise BotBusy("Подтверждённый процесс не найден")
            if force:
                # A launcher may delegate to a worker with a different PID on
                # any OS.  Killing only Popen leaves that worker alive while
                # the control plane believes the bot has stopped.  Use the
                # same ownership verification and process-tree termination on
                # Windows and POSIX; never signal an unverified PID.
                self.kill_owned_launcher(child, folder)
                return
        if force:
            descendants = proc.children(recursive=True)
            for child in reversed(descendants):
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            try:
                proc.kill()
            except psutil.NoSuchProcess:
                pass
        else:
            if proc is None:
                token = self.tokens.get(str(folder))
            else:
                snapshot = load_json(folder / "process.json")
                if (
                    snapshot["pid"] != proc.pid
                    or abs(snapshot["created"] - proc.create_time()) > 0.01
                ):
                    raise BotBusy("Процесс изменился во время команды остановки")
                token = snapshot["token"]
            if not token:
                raise BotBusy("Не подтверждён запуск бота")
            atomic_json(
                folder / "stop.request", {"id": uuid.uuid4().hex, "token": token}
            )

    def kill_owned_launcher(self, child, folder):
        """Before a worker snapshot exists, confirm the owned launcher's args."""
        proc = psutil.Process(child.pid)
        args = proc.cmdline()
        try:
            confirmed = (
                args[args.index("--token") + 1] == self.tokens.get(str(folder))
                and Path(args[args.index("--folder") + 1]).resolve() == folder
            )
        except (ValueError, IndexError):
            confirmed = False
        if child.poll() is not None or not confirmed:
            raise BotBusy("Не подтверждён запускающий процесс бота")
        for descendant in reversed(proc.children(recursive=True)):
            try:
                descendant.kill()
            except psutil.NoSuchProcess:
                pass
        try:
            child.kill()
        except ProcessLookupError:
            pass

    def finish(self, folder):
        folder = self.repo.resolve(folder)
        if self.active_snapshot(folder) is None:
            raise BotBusy("Подтверждённый процесс не найден")
        snapshot = self.active_snapshot(folder)
        if snapshot is None or not snapshot.get('token'):
            raise BotBusy("Невозможно подтвердить владельца команды завершения")
        atomic_json(
            folder / "finish.request",
            {"id": uuid.uuid4().hex, "draining": True, "token": snapshot['token']}
        )

    def cancel_finish(self, folder):
        folder = self.repo.resolve(folder)
        if self.active_snapshot(folder) is None:
            raise BotBusy("Подтверждённый процесс не найден")
        snapshot = self.active_snapshot(folder)
        if snapshot is None or not snapshot.get('token'):
            raise BotBusy("Невозможно подтвердить владельца отмены завершения")
        atomic_json(
            folder / "finish.request",
            {"id": uuid.uuid4().hex, "draining": False, "token": snapshot['token']}
        )

    def close_positions(self, folder):
        """Deny the legacy bypass around the durable operator command plane.

        Only Supervisor.submit_manual_close may admit an operator request.
        This prevents callers from directly writing a worker command file
        without registry, launch-revision and retirement checks.
        """
        raise BotBusy(
            "Ручное закрытие подаётся через Supervisor.submit_manual_close"
        )
