"""Atomic, independently verified, offline transfers of PAPER bots."""

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from contextlib import ExitStack, nullcontext
from pathlib import Path, PurePosixPath

from .config import STRATEGIES
from .storage import BotLock, Store, guard_paper_recovery, recover_deposit_change, IMPORT_MARKER
from .launch_claim import check_launch_lease

LOG_FILES = ("engine.log", "console.log")
PREFIX = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


def export_bots(repo, folders, target, history=True, *, owner_gate=None,
                source_owner_batch=None):
    """Export one coherent multi-owner snapshot under a fleet-wide admission fence.

    A per-bot gate cannot protect an earlier snapshot from START/EDIT while
    later bots are still being copied. The Supervisor supplies a canonical
    batch gate; all OS BotLocks remain held through the last snapshot. Direct
    legacy callers retain their individual admission contract.
    """
    target = Path(target).resolve()
    if target.suffix.lower() != ".zip" or target.is_relative_to(repo.bots):
        raise ValueError("Сохраните ZIP переноса вне папки MY_BOTS")
    sources = tuple(repo.resolve(folder) for folder in folders)
    if len(sources) != len(set(sources)):
        raise ValueError("Повторяющийся бот в пакете экспорта")
    with tempfile.TemporaryDirectory(prefix=".bcc-export-", dir=target.parent) as temp:
        temp = Path(temp)
        pending = temp / "export.zip"
        manifest = []
        with ExitStack() as batch, zipfile.ZipFile(pending, "w", zipfile.ZIP_DEFLATED) as archive:
            if source_owner_batch is not None:
                # Same canonical stripe/OS-gate ordering as import and edit.
                # The batch owns every BotLock before any archive bytes are read.
                batch.enter_context(source_owner_batch(sources))
                for folder in sorted(sources, key=lambda path: path.as_posix()):
                    batch.enter_context(BotLock(folder))
                    check_launch_lease(folder)
                    recover_deposit_change(folder)
            for i, folder in enumerate(sources):
                # In batch mode, these guards remain held for the *entire*
                # archive. Legacy standalone transfers retain per-bot gates.
                gate = (owner_gate(folder) if source_owner_batch is None and
                        owner_gate is not None else nullcontext())
                lock = BotLock(folder) if source_owner_batch is None else nullcontext()
                with gate, lock:
                    if source_owner_batch is None:
                        check_launch_lease(folder)
                        recover_deposit_change(folder)
                    cfg, _ = repo.load(folder)
                    has_ledger = guard_paper_recovery(folder)
                    prefix = f"bot_{i:04d}"
                    archive.writestr(
                        prefix + "/config.json",
                        json.dumps(cfg, ensure_ascii=False, allow_nan=False),
                    )
                    if history and has_ledger:
                        snapshot_dir = temp / prefix
                        snapshot_dir.mkdir()
                        backup = snapshot_dir / "paper.sqlite"
                        Store(folder).backup(backup)
                        # Audit the *snapshot*, not just the original: validate
                        # every copy independently before publishing any ZIP.
                        Store(snapshot_dir).audit()
                        archive.write(backup, prefix + "/paper.sqlite")
                    if history:
                        for name in LOG_FILES:
                            if (folder / name).is_file():
                                archive.write(folder / name, prefix + "/" + name)
                    manifest.append({
                        "prefix": prefix, "ledger_present": has_ledger,
                        # Reference only a known local bot inside this MY_BOTS;
                        # never trust/import an arbitrary filesystem path.
                        "source_bot_id": folder.name,
                        "source_strategy": cfg["strategy"],
                        "source_config_sha256": hashlib.sha256(
                            (folder / "config.json").read_bytes()).hexdigest(),
                    })
            archive.writestr(
                "manifest.json", json.dumps({"format": 1, "includes_history": bool(history),
                                             "bots": manifest}),
            )
        os.replace(pending, target)


def import_bots(repo, source, history=True, *, source_owner_batch=None):
    """Stage and validate the complete batch before exposing any imported bot."""
    staged = []
    published = []
    batch = uuid.uuid4().hex
    commit_dir = repo.bots / ".import-commits"
    owners = ExitStack()
    try:
        with zipfile.ZipFile(source) as archive:
            infos = archive.infolist()
            if (len(infos) > 5001 or
                    sum(i.file_size for i in infos) > 500_000_000):
                raise ValueError("Слишком большой архив переноса")
            members = archive.namelist()
            if len(members) != len(set(members)):
                raise ValueError("Повторяющиеся файлы в архиве")
            for name in members:
                p = PurePosixPath(name)
                if p.is_absolute() or ".." in p.parts or "\\" in name:
                    raise ValueError("Небезопасный путь в архиве")
            manifest = json.loads(archive.read("manifest.json"))
            if (not isinstance(manifest, dict) or manifest.get("format") != 1
                    or not isinstance(manifest.get("bots"), list)):
                raise ValueError("Неизвестный формат переноса")
            if history and manifest.get("includes_history") is False:
                raise ValueError("Архив не содержит историю. Выберите импорт только настроек")
            seen, prepared, originals = set(), [], set()
            for entry in manifest["bots"]:
                if not isinstance(entry, dict):
                    raise ValueError("Некорректная запись бота в архиве")
                prefix = entry.get("prefix")
                if not isinstance(prefix, str) or not PREFIX.fullmatch(prefix) or prefix in seen:
                    raise ValueError("Неверный или повторяющийся каталог бота")
                seen.add(prefix)
                cfg = json.loads(archive.read(prefix + "/config.json"))
                if not isinstance(cfg, dict):
                    raise ValueError("Повреждена конфигурация бота в архиве")
                # Only check local matching originals; old source_path strings
                # (which may name any outside folder) are never dereferenced.
                local_id = entry.get("source_bot_id")
                local_hash = entry.get("source_config_sha256")
                if (isinstance(local_id, str) and
                    re.fullmatch(r"[0-9a-f]{12}", local_id) and
                    isinstance(cfg.get("strategy"), str) and
                    cfg["strategy"] in STRATEGIES and
                    entry.get("source_strategy") == cfg["strategy"] and
                    isinstance(local_hash, str) and
                    re.fullmatch(r"[0-9a-f]{64}", local_hash)):
                    origin = repo.bots / cfg["strategy"] / local_id
                    if origin.is_dir():
                        origin = repo.resolve(origin)
                        # A changed configuration hash does NOT make the same
                        # local source bot safe to clone while it is running.
                        # The exporter recorded the immutable local bot ID.
                        # A stale hash merely means it was edited since export.
                        originals.add(origin)
                prepared.append((entry, prefix, cfg))

            # Admit the entire set before staging anything, in canonical order.
            # Keep both the command and worker fences through publication AND
            # rollback. A preflight-only lock lets START or deletion overtake
            # the import while its ledger is still being validated/published.
            originals = sorted(originals, key=lambda path: path.as_posix())
            if source_owner_batch is not None:
                owners.enter_context(source_owner_batch(originals))
            for origin in originals:
                owners.enter_context(BotLock(origin))
                check_launch_lease(origin)

            for entry, prefix, cfg in prepared:
                cfg["name"] = cfg["name"][: 80 - len(" — импорт")] + " — импорт"
                ledger_member = prefix + "/paper.sqlite"
                has_ledger = ledger_member in members
                declared = entry.get("ledger_present")
                if history and (declared is True and not has_ledger or
                                declared is False and has_ledger):
                    raise ValueError("Состав истории не совпадает с манифестом")
                if history and declared is None and not has_ledger:
                    # Legacy ZIP has no evidence an account was never started.
                    raise ValueError("Старая копия не подтверждает полноту PAPER-истории")
                staging = repo.prepare(cfg)
                staged.append(staging)
                # This tag travels with the atomic staging->final rename.
                # A missing commit means no part of this batch is runnable.
                (staging / IMPORT_MARKER).write_text(batch, encoding="ascii")
                if history and has_ledger:
                    # zipfile extracts the raw bytes only, never archive paths.
                    content = archive.read(ledger_member)
                    if not content.startswith(b"SQLite format 3\0"):
                        raise ValueError("Некорректная история PAPER")
                    (staging / "paper.sqlite").write_bytes(content)
                    store = Store(staging, cfg["capital"])
                    store.audit()
                if history:
                    for name in LOG_FILES:
                        member = prefix + "/" + name
                        if member in members:
                            (staging / name).write_bytes(archive.read(member))
            # All renames are private until one atomic commit-file rename.
            for staging in staged:
                published.append(repo.publish(staging))
            if published:
                commit_dir.mkdir(exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="ascii", dir=commit_dir,
                    prefix=".pending-", delete=False,
                ) as output:
                    temp_commit = Path(output.name)
                    output.write(batch + "\n")
                    output.flush()
                    os.fsync(output.fileno())
                try:
                    os.replace(temp_commit, commit_dir / batch)
                finally:
                    temp_commit.unlink(missing_ok=True)
        # Keep the batch marker as a durable audit witness. A listing captures
        # the committed IDs once and cannot observe only half a committed batch.
        return published
    except BaseException:
        # A rename could have succeeded before a patched or failing publish()
        # raised: find tagged targets as well as the returned paths.
        suspects = set(published)
        for path in repo.bots.glob("*/*/" + IMPORT_MARKER):
            try:
                if path.read_text(encoding="ascii").strip() == batch:
                    suspects.add(path.parent)
            except (OSError, UnicodeError):
                pass
        for path in suspects:
            if not path.exists():
                continue
            # Rename first, so even a failed/partial rmtree cannot expose a bot.
            try:
                quarantine = path.with_name(".quarantined-" + uuid.uuid4().hex)
                path.rename(quarantine)
                path = quarantine
            except OSError:
                # Keep the intact uncommitted marker. Partial deletion could
                # remove the marker first and expose a damaged account.
                continue
            try:
                shutil.rmtree(path)
            except OSError:
                pass
        raise
    finally:
        try:
            for path in staged:
                if path.exists():
                    try:
                        shutil.rmtree(path)
                    except OSError:
                        pass
        finally:
            owners.close()
