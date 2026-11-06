"""Tests for isolated AI development pipeline; no external models/keys required."""
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from tools import ai_dev_pipeline as p


def test_sensitive_path_guards():
    for path in ('MY_BOTS/foo.ini', 'TEST_DATA/accounts.sqlite', '.env',
                 'client_secret.json', 'some/location/data.sqlite3', '../escape',
                 '/etc/passwd', 'paper_accounts.sqlite3'):
        with pytest.raises(p.Blocked):
            p.validate_paths([path])
    p.validate_paths(['bcc/worker.py', 'tests/test_new.py', 'tools/new_runner.py'])


def test_ollama_must_be_loopback():
    with pytest.raises(p.Blocked):
        p.local_request('http://any.public.host:11434', '/api/tags')
    with pytest.raises(p.Blocked):
        p.local_request('https://127.0.0.1:11434', '/api/tags')
    with pytest.raises(p.Blocked):
        p.local_request('http://127.0.0.1:11434@untrusted.example', '/api/tags')


def test_local_ollama_lists_models(monkeypatch):
    monkeypatch.setattr(p, 'local_request', lambda url, path, **kw: {'models': [{'name': 'qwen3:8b'}]})
    assert p.local_ollama('http://127.0.0.1:11434') == ['qwen3:8b']


@pytest.mark.parametrize('content,blocked', [
    ('{"blocking_findings": [], "nonblocking_findings": [], "review_complete": true}', False),
    ('{"blocking_findings": [{"file":"bcc/worker.py","issue":"race","evidence":"line 10"}], "nonblocking_findings": [], "review_complete": true}', True),
])
def test_review_schema(monkeypatch, content, blocked):
    monkeypatch.setattr(p, 'local_ollama', lambda *a, **kw: ['qwen3:8b'])
    monkeypatch.setattr(p, 'local_request', lambda *a, **kw: {'response': content})
    result = p.ollama_review(url='http://127.0.0.1:11434', model='qwen3:8b',
                             diff='diff --git', mission='test')
    assert bool(result['blocking_findings']) is blocked


@pytest.mark.parametrize('content', ['not json', '{}',
    '{"blocking_findings": [], "review_complete": true}',
    '{"blocking_findings": [], "nonblocking_findings": [], "review_complete": false}'])
def test_invalid_review_fails_closed(monkeypatch, content):
    monkeypatch.setattr(p, 'local_ollama', lambda *a, **kw: ['qwen3:8b'])
    monkeypatch.setattr(p, 'local_request', lambda *a, **kw: {'response': content})
    with pytest.raises(p.Blocked):
        p.ollama_review(url='http://127.0.0.1:11434', model='qwen3:8b', diff='diff', mission='test')


def test_oversized_diff_is_not_silently_truncated(monkeypatch):
    with pytest.raises(p.Blocked, match='слишком большой'):
        p.ollama_review(url='http://127.0.0.1:11434', model='qwen3:8b',
                        diff='a' * (p.REVIEW_MAX_BYTES + 1), mission='test')


def test_git_diff_includes_new_files(tmp_path, monkeypatch):
    src = tmp_path / 'repo'
    src.mkdir()
    subprocess.run(['git', 'init', '-q', str(src)], check=True)
    subprocess.run(['git', '-C', str(src), 'config', 'user.email', 'test@example.invalid'], check=True)
    subprocess.run(['git', '-C', str(src), 'config', 'user.name', 'Test'], check=True)
    (src / '.gitignore').write_text('MY_BOTS/\n', encoding='utf-8')
    (src / 'existing.txt').write_text('old\n', encoding='utf-8')
    subprocess.run(['git', '-C', str(src), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(src), 'commit', '-qm', 'baseline'], check=True)
    base = subprocess.check_output(['git', '-C', str(src), 'rev-parse', 'HEAD'], text=True).strip()
    (src / 'new.py').write_text('def value(): return 123\n', encoding='utf-8')
    (src / 'existing.txt').write_text('new\n', encoding='utf-8')
    (src / 'MY_BOTS').mkdir()
    (src / 'MY_BOTS' / 'private.sqlite3').write_bytes(b'never include')
    monkeypatch.setattr(p, 'ROOT', src)
    diff, files = p.diff_from(base)
    assert sorted(files) == ['existing.txt', 'new.py']
    assert 'new.py' in diff and 'private.sqlite3' not in diff


def test_doctor_blocked_if_dependencies_missing(monkeypatch):
    monkeypatch.setattr(p.shutil, 'which', lambda name: None)
    monkeypatch.setattr(p, 'local_ollama', lambda *a, **kw: ['qwen3:8b'])
    result = p.doctor('qwen3:8b', 'http://127.0.0.1:11434')
    assert not result['ready_for_ai_cycle']


def test_windows_qa_rejected_on_linux(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(p, 'os', SimpleNamespace(name='posix'))
    # Inject seal so stage reaches OS check without creating a test release.
    from tools import seal_source
    def fake_seal(root, archive, *, checkpoint):
        with zipfile.ZipFile(archive,'w') as z:
            z.writestr(checkpoint+'/hello.txt', 'hello')
    monkeypatch.setattr(seal_source, 'seal', fake_seal)
    with pytest.raises(p.Blocked, match='Windows'):
        p.seal_and_qa(tmp_path, 'windows')


def test_cycle_always_drops_lock_and_writes_error(monkeypatch, tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    (root / 'mission.md').write_text('goals ' * 100, encoding='utf-8')
    monkeypatch.setattr(p, 'ROOT', root)
    monkeypatch.setattr(p, 'WORK', root / '.ai_work')
    monkeypatch.setattr(p, 'doctor', lambda *a, **kw: {'ready_for_ai_cycle': True})
    monkeypatch.setattr(p, 'repo_check', lambda: 'base123')
    monkeypatch.setattr(p, 'git', lambda *a, **kw: '')
    def fail(*args, **kw):
        raise p.Blocked('Codex failed')
    monkeypatch.setattr(p, 'run_codex', fail)
    monkeypatch.setattr(p, 'codex_write_probe', lambda *a, **kw: {'status': 'PASS'})
    args = type('A', (), dict(mission=root/'mission.md', model='qwen3:8b',
                             url='http://127.0.0.1:11434', codex_timeout=2, qa='none'))()
    with pytest.raises(p.Blocked):
        p.cycle(args)
    assert not (p.WORK / 'cycle.lock').exists()
    results = list(p.WORK.glob('*/status.json'))
    assert len(results) == 1
    data = json.loads(results[0].read_text(encoding='utf-8'))
    assert data['release_ready'] is False
    assert 'Codex failed' in data['error']


def test_cycle_success_keeps_code_in_separate_branch_and_never_releases(tmp_path, monkeypatch):
    root = tmp_path / 'repo'
    root.mkdir()
    mission = root / 'mission.md'
    mission.write_text('architecture and paper acceptance ' * 15, encoding='utf-8')
    (root / '.gitignore').write_text('.ai_work/\n', encoding='utf-8')
    (root / 'src.py').write_text('old = True\n', encoding='utf-8')
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    subprocess.run(['git', '-C', str(root), 'config', 'user.email', 'test@example.invalid'], check=True)
    subprocess.run(['git', '-C', str(root), 'config', 'user.name', 'Test'], check=True)
    subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(root), 'commit', '-qm', 'baseline'], check=True)
    monkeypatch.setattr(p, 'ROOT', root)
    monkeypatch.setattr(p, 'WORK', root / '.ai_work')
    monkeypatch.setattr(p, 'doctor', lambda *a, **kw: {'ready_for_ai_cycle': True})
    monkeypatch.setattr(p, 'run_codex', lambda prompt, *, log, timeout: (root / 'src.py').write_text('old = False\n', encoding='utf-8'))
    monkeypatch.setattr(p, 'codex_write_probe', lambda *a, **kw: {'status': 'PASS'})
    monkeypatch.setattr(p, 'ollama_review', lambda **kw: {'blocking_findings': [], 'nonblocking_findings': [], 'review_complete': True})
    monkeypatch.setattr(p, 'seal_and_qa', lambda run_dir, qa, base=None: {'qa': 'NOT_RUN', 'release_ready': False})
    args = type('A', (), dict(mission=mission, model='qwen3:8b',
                             url='http://127.0.0.1:11434', codex_timeout=2, qa='none'))()
    result = p.cycle(args)
    assert result['codex'] == 'DONE'
    assert result['ollama'] == 'REVIEW_NO_BLOCKERS_NOT_A_CERTIFICATION'
    assert result['release_ready'] is False
    assert result['changed_files'] == ['src.py']
    assert result['branch'].startswith('ai/bcc-')
    assert not (p.WORK / 'cycle.lock').exists()
    assert subprocess.check_output(['git', '-C', str(root), 'branch', '--show-current'], text=True).strip() == result['branch']
    assert (Path(result['run_dir']) / 'changes.diff').is_file()
