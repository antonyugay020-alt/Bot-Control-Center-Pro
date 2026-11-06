"""Create an immutable, clean BCC source snapshot for Windows acceptance.

This is intentionally *not* a release builder. The same manifest is checked by
Windows preflight and by this builder after extraction. No generated test files,
workspaces, credentials, bot accounts or private SQLite databases are copied.
"""
import argparse
import hashlib
import json
import tempfile
import zipfile
from pathlib import Path
from tools.verify_package import REQUIRED_FILES, verify

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINTS = (
    '.gitignore', 'AI_SETUP_WINDOWS.bat', 'AI_CYCLE_WINDOWS.bat',
    'AI_API_LOGIN_WINDOWS.bat', 'AI_USE_CHATGPT_WINDOWS.bat', 'AI_USE_EXISTING_API_WINDOWS.bat',
    'AI_API_LOGIN_WINDOWS.bat', 'AI_USE_CHATGPT_WINDOWS.bat',
    'BCC_AI_MISSION_RU.md', 'AI_DEVELOPMENT_RU.md',
    'INTERNAL_QA_STATUS_20261105.json', 'PAPER_SNAPSHOT_INTERNAL_AUDIT_20261105.json',
    'RUN23_INTERNAL_ARCHITECTURE_NOTES_RU.md',
    'RUN24_INTERNAL_ARCHITECTURE_NOTES_RU.md', 'RUN25_INTERNAL_ARCHITECTURE_NOTES_RU.md',
    'INTERNAL_QA_STATUS_20261106.json',
    'RUN26_INTERNAL_ARCHITECTURE_NOTES_RU.md',
    'RUN27_INTERNAL_ARCHITECTURE_NOTES_RU.md', 'RUN27_INTERNAL_QA_STATUS.json',
    'RUN29_INTERNAL_ARCHITECTURE_NOTES_RU.md', 'RUN29_INTERNAL_QA_STATUS.json',
    'RUN30_INTERNAL_ARCHITECTURE_NOTES_RU.md', 'RUN30_INTERNAL_QA_STATUS.json',
    'INTEGRATION_REVIEW_RU.md', 'REQUIREMENTS_MATRIX.json',
    'ACCEPTANCE_WINDOWS_RU.md', 'ARCHITECTURE_ACTUAL_STATUS_RU.md',
    'ARCHITECTURE_TARGET_UNIVERSAL_RU.md', 'BUILD_INFO.json',
    'CHECK_WINDOWS.bat', 'INSTALL.bat', 'README_RU.md',
    'RECOVERY_RU.md', 'REPORT_NOW.bat', 'REQUIREMENTS_RU.md',
    'VALIDATION_STATUS.json',
    'START.bat', 'START_DIAGNOSTIC.bat', 'START_HERE_VISUAL_RU.md', 'STATUS_RU.md',
    'TEST_START.bat', 'VALIDATE_WINDOWS.bat', 'WINDOWS_CHECK_RU.md',
    'app.py', 'launch.pyw', 'requirements.txt', 'requirements-dev.txt',
    'tools/start_windows_check.ps1',
)


def source_paths(root):
    """Enumerate tracked executable content, not transient filesystem debris."""
    root = Path(root).resolve()
    names = set(ENTRYPOINTS) | set(REQUIRED_FILES)
    for folder in ('bcc', 'tests', 'tools'):
        for path in (root / folder).rglob('*.py'):
            rel = path.relative_to(root)
            if any(part.startswith('.') or part == '__pycache__' for part in rel.parts):
                continue
            names.add(rel.as_posix())
    for name in sorted(names):
        path = root / name
        if not path.is_file() or path.is_symlink() or any(
                parent.is_symlink() for parent in path.parents if parent != root and parent.is_relative_to(root)):
            raise ValueError('Missing or linked source: ' + name)
        yield name, path


def seal(root, destination, *, checkpoint):
    root, destination = Path(root).resolve(), Path(destination).resolve()
    if not checkpoint or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in checkpoint):
        raise ValueError('Invalid unique package root')
    mapping = dict(source_paths(root))
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in mapping.items()}
    manifest = {'checkpoint': checkpoint, 'files': hashes}
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='bcc-sealed-') as scratch:
        staged = Path(scratch) / checkpoint
        staged.mkdir()
        for name, path in mapping.items():
            target = staged / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
        (staged / 'SHA256_MANIFEST.json').write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + '\n', encoding='utf-8')
        assert verify(staged)['verified_files'] == len(mapping)
        with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in sorted(staged.rglob('*')):
                if path.is_file():
                    member = path.relative_to(staged.parent).as_posix()
                    archive.write(path, member)
        # Verify the *actual* archive, not only staging inputs.
        with tempfile.TemporaryDirectory(prefix='bcc-reopen-') as unpack:
            with zipfile.ZipFile(destination) as archive:
                archive.extractall(unpack)
            verified = verify(Path(unpack) / checkpoint)
            if verified['verified_files'] != len(mapping):
                raise ValueError('Reopened archive differs from source snapshot')
    return {'package': str(destination), 'source_files': len(mapping), 'checkpoint': checkpoint}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--checkpoint', required=True)
    args = parser.parse_args(argv)
    print(json.dumps(seal(args.root, args.output, checkpoint=args.checkpoint), ensure_ascii=False))


if __name__ == '__main__':
    main()
