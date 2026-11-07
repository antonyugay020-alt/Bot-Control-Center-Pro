"""Regression for Windows Git 'Author identity unknown' and half-initialized BCC checkout.

Real Git subprocesses; never use the user's global Git identity, existing sources,
PAPER accounts, or destructive reset commands. These are Linux-runnable QA tests,
not a claim that the Windows GUI has been validated.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from tools import ai_dev_pipeline as pipeline
from tools.verify_package import SourceIntegrityError, verify


def git(root, *args, check=True):
    return subprocess.run(['git', '-C', str(root), *args], capture_output=True,
                          text=True, check=check)


@pytest.fixture
def clean_bcc(tmp_path, monkeypatch):
    original = Path(__file__).resolve().parents[1]
    target = tmp_path / 'BCC source with spaces'
    target.mkdir()
    manifest = json.loads((original / 'SHA256_MANIFEST.json').read_text(encoding='utf-8'))
    for name in [*manifest['files'], 'SHA256_MANIFEST.json']:
        dst = target / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original / name, dst)
    assert verify(target)['status'] == 'PASS'
    # A reproducible machine with NO configured global/system Git identity.
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(tmp_path / 'empty-global-gitconfig'))
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.delenv('GIT_AUTHOR_NAME', raising=False)
    monkeypatch.delenv('GIT_AUTHOR_EMAIL', raising=False)
    monkeypatch.delenv('GIT_COMMITTER_NAME', raising=False)
    monkeypatch.delenv('GIT_COMMITTER_EMAIL', raising=False)
    monkeypatch.setattr(pipeline, 'ROOT', target)
    monkeypatch.setattr(pipeline, 'WORK', target / '.ai_work')
    return target


def test_fresh_init_needs_no_personal_git_identity(clean_bcc):
    root = clean_bcc
    result = pipeline.init_repo()
    assert result['initialized'] is True
    assert result['recovered'] is False
    assert git(root, 'config', '--local', 'user.name').stdout.strip() == 'BCC Local Automation'
    assert git(root, 'config', '--local', 'user.email').stdout.strip() == 'bcc-automation@localhost.invalid'
    assert git(root, 'status', '--porcelain').stdout.strip() == ''
    assert pipeline.repo_check() == result['head']
    assert len(git(root, 'ls-files').stdout.splitlines()) == len(json.loads((root / 'SHA256_MANIFEST.json').read_text(encoding='utf-8'))['files']) + 1
    assert not (root / 'MY_BOTS').exists()


def test_resumes_unborn_git_with_preexisting_partial_staging(clean_bcc):
    root = clean_bcc
    git(root, 'init', '-q')
    # Reproduce previous failure: some/all sources staged, HEAD absent.
    git(root, 'add', '--', '.gitignore', 'SHA256_MANIFEST.json')
    assert git(root, 'rev-parse', '--verify', 'HEAD', check=False).returncode != 0
    result = pipeline.init_repo()
    assert result['initialized'] is True
    assert result['recovered'] is True
    assert pipeline.repo_check() == result['head']
    assert git(root, 'status', '--porcelain').stdout.strip() == ''
    again = pipeline.init_repo()
    assert again['initialized'] is False
    assert again['head'] == result['head']


def test_existing_identity_not_overridden(clean_bcc):
    root = clean_bcc
    git(root, 'init', '-q')
    git(root, 'config', '--local', 'user.name', 'Existing Developer')
    git(root, 'config', '--local', 'user.email', 'existing@example.invalid')
    pipeline.init_repo()
    assert git(root, 'config', '--local', 'user.name').stdout.strip() == 'Existing Developer'
    assert git(root, 'config', '--local', 'user.email').stdout.strip() == 'existing@example.invalid'


def test_refuses_unexpected_staged_files_and_preserves_them(clean_bcc):
    root = clean_bcc
    git(root, 'init', '-q')
    (root / 'custom-user-note.txt').write_text('do not destroy', encoding='utf-8')
    git(root, 'add', '--', 'custom-user-note.txt')
    with pytest.raises(SourceIntegrityError, match='Незарегистрированный файл исходников: custom-user-note.txt'):
        pipeline.init_repo()
    assert (root / 'custom-user-note.txt').read_text(encoding='utf-8') == 'do not destroy'
    assert 'custom-user-note.txt' in git(root, 'ls-files').stdout
    assert git(root, 'rev-parse', '--verify', 'HEAD', check=False).returncode != 0


def test_refuses_unexpected_untracked_files_without_deleting(clean_bcc):
    root = clean_bcc
    git(root, 'init', '-q')
    extra = root / 'untracked-unknown-file.txt'
    extra.write_text('private material', encoding='utf-8')
    with pytest.raises(SourceIntegrityError, match='Незарегистрированный файл исходников: untracked-unknown-file.txt'):
        pipeline.init_repo()
    assert extra.read_text(encoding='utf-8') == 'private material'


def test_existing_dirty_committed_repository_not_reset(clean_bcc):
    root = clean_bcc
    result = pipeline.init_repo()
    tracked = root / 'README_RU.md'
    tracked.write_text(tracked.read_text(encoding='utf-8') + '\nlocal changes\n', encoding='utf-8')
    with pytest.raises((pipeline.Blocked, ValueError), match='SHA-256|изменено'):
        pipeline.init_repo()
    assert 'local changes' in tracked.read_text(encoding='utf-8')
    assert git(root, 'rev-parse', '--verify', 'HEAD').stdout.strip() == result['head']


def test_refuses_staged_copy_different_from_source(clean_bcc):
    root = clean_bcc
    git(root, 'init', '-q')
    tracked = root / '.gitignore'
    git(root, 'add', '--', '.gitignore')
    # Simulate a changed *staged blob* while working copy remains sealed.
    git(root, 'hash-object', '-w', '--stdin', check=True)  # harmless empty blob
    blob = subprocess.run(['git', '-C', str(root), 'hash-object', '-w', '--stdin'],
                          input='malicious staged replacement\n', capture_output=True, text=True, check=True).stdout.strip()
    git(root, 'update-index', '--cacheinfo', f'100644,{blob},.gitignore')
    with pytest.raises(pipeline.Blocked, match='отличная от проверенных'):
        pipeline.init_repo()
    assert git(root, 'rev-parse', '--verify', 'HEAD', check=False).returncode != 0
