"""Fail-closed verification of the exact source snapshot before native QA.

The manifest intentionally excludes itself to avoid circular hashes. Generated
venvs, logs and test workspaces are not part of the verified source snapshot.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
SHA256 = re.compile(r"^[0-9a-f]{64}$")

# Every entry point and every core trading/storage subsystem must be sealed.
# Requiring only the checker itself can falsely pass a package from which a
# launcher or an essential engine module was removed along with its manifest row.
REQUIRED_FILES = (
    'tools/ai_dev_pipeline.py', 'tools/windows_ai_launcher.py', 'AI_SETUP_WINDOWS.bat',
    'AI_CYCLE_WINDOWS.bat', 'AI_USE_EXISTING_API_WINDOWS.bat', 'AI_DEVELOPMENT_RU.md', 'BCC_AI_MISSION_RU.md',
    "app.py", "launch.pyw", "BUILD_INFO.json", "requirements.txt",
    "requirements-dev.txt", "START.bat", "START_DIAGNOSTIC.bat", "TEST_START.bat", "REPORT_NOW.bat", "INSTALL.bat",
    "VALIDATE_WINDOWS.bat", "CHECK_WINDOWS.bat",
    "tools/start_windows_check.ps1", "tools/windows_check.py",
    "tools/verify_windows.py", "tools/verify_package.py",
    "tools/process_guard.py", "tools/qa_budgets.py", "tools/repeat_full_qa.py",
    "tools/collect_test_inventory.py", "tools/export_test_report.py",
    "bcc/__init__.py", "bcc/bootstrap.py", "bcc/storage.py", "bcc/runtime.py",
    "bcc/trading.py", "bcc/worker.py", "bcc/market.py",
    "bcc/config.py", "bcc/constructor_session.py", "bcc/constructor_transaction.py", "bcc/transfer.py", "bcc/lifecycle.py",
    "bcc/ui.py", "bcc/gui_check.py", "bcc/health.py",
    "bcc/margin.py", "bcc/liquidation.py", "bcc/shared_risk.py",
    "bcc/runtime_process.py", "bcc/runtime_service.py", "bcc/coordination.py",
    "bcc/process_os.py", "bcc/position_journal.py",
    "tools/acceptance_session.py",
    "tools/qa_locale.py",
)



class SourceIntegrityError(ValueError):
    pass


def verify(root=ROOT):
    root = Path(root).resolve()
    manifest_path = root / "SHA256_MANIFEST.json"
    try:
        if manifest_path.is_symlink():
            raise SourceIntegrityError('Манифест исходников не может быть ссылкой')
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, ValueError, UnicodeError) as exc:
        raise SourceIntegrityError("Невозможно прочитать SHA256_MANIFEST.json") from exc
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, dict) or len(files) < 30:
        raise SourceIntegrityError("Пустой или повреждённый манифест исходников")
    checked = set()
    for key, digest in files.items():
        if not isinstance(key, str) or not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise SourceIntegrityError("Некорректная строка манифеста")
        rel = PurePosixPath(key)
        if rel.is_absolute() or ".." in rel.parts or "\\" in key or key != rel.as_posix():
            raise SourceIntegrityError("Недопустимый путь манифеста")
        path = root.joinpath(*rel.parts)
        if path.is_symlink() or not path.is_file():
            raise SourceIntegrityError("Отсутствует файл или обнаружена ссылка: " + key)
        # Checking only resolve().is_relative_to(root) is insufficient: a
        # symlinked parent could redirect to another file INSIDE the package
        # and silently change an executable after verification.
        if any(parent.is_symlink() for parent in (path, *path.parents) if parent != root
               and parent.is_relative_to(root)):
            raise SourceIntegrityError("Ссылка в пути исходника: " + key)
        if not path.resolve().is_relative_to(root):
            raise SourceIntegrityError("Файл выходит за корень пакета: " + key)
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != digest:
            raise SourceIntegrityError("Не совпадает SHA-256: " + key)
        checked.add(key)
    # Seal all executable entrypoints and automation definitions, not just
    # previously known names. A new launcher, PowerShell script, Python module
    # or CI workflow must never bypass the manifest through an unlisted path.
    # Generated paths are excluded; no application code should live there.
    executable_suffixes = {".py", ".pyw", ".bat", ".cmd", ".ps1"}
    automation_dirs = {".github/workflows"}
    excluded_dirs = {".git", ".ai_work", ".venv", "venv", "__pycache__",
                     ".pytest_cache", "check_workspaces", "node_modules"}
    for path in root.rglob("*"):
        if not path.is_file() and not path.is_symlink():
            continue
        relative = path.relative_to(root)
        if any(part in excluded_dirs for part in relative.parts):
            continue
        rel = relative.as_posix()
        is_automation = any(rel.startswith(folder + "/") for folder in automation_dirs)
        if path.suffix.lower() in executable_suffixes or (is_automation and path.suffix.lower() in {".yml", ".yaml"}):
            if rel not in checked:
                raise SourceIntegrityError("Незарегистрированный исполняемый файл: " + rel)
    for required in REQUIRED_FILES:
        if required not in checked:
            raise SourceIntegrityError("Неучтён обязательный файл: " + required)
    if manifest_path.read_bytes() != manifest_bytes:
        raise SourceIntegrityError('Манифест изменился во время проверки')
    return {"status": "PASS", "verified_files": len(checked), "checkpoint": manifest.get("checkpoint"),
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest()}


def assert_same_snapshot(expected, actual):
    """A newly resealed source cannot inherit an earlier stage's acceptance."""
    if expected != actual:
        raise SourceIntegrityError('Исходники или манифест изменились во время приёмки; '
                                   'распакуйте пакет в новую папку и повторите проверку')
    return actual


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        result = verify(args.root)
    except SourceIntegrityError as exc:
        print("SOURCE INTEGRITY FAIL:", exc, flush=True)
        return 1
    print("SOURCE INTEGRITY PASS:", result, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
