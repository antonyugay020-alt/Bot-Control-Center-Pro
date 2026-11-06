"""Write-capability gate for Codex, with no real model calls or credentials."""
from pathlib import Path
from types import SimpleNamespace
import json
import re

import pytest
from tools import ai_dev_pipeline as p


def _setup(monkeypatch, tmp_path):
    monkeypatch.setattr(p, 'ROOT', tmp_path)
    monkeypatch.setattr(p, 'WORK', tmp_path / '.ai_work')
    monkeypatch.setattr(p, 'codex_backend', lambda: ('node-direct', ['node', 'codex.js'], []))
    monkeypatch.setattr(p, '_codex_environment', lambda mode=None: {'PYTHONUTF8': '1'})
    monkeypatch.setattr(p, 'git', lambda *a, **kw: '')
    r = p.WORK / 'run_0001'
    r.mkdir(parents=True)
    return r


def test_real_write_proof_requires_exact_model_file(tmp_path, monkeypatch):
    r = _setup(monkeypatch, tmp_path)
    def fake_process(argv, **kw):
        assert argv == ['node', 'codex.js', 'exec', '--sandbox', 'workspace-write', '-c', 'sandbox_mode="workspace-write"', '-']
        assert kw['env'] == {'PYTHONUTF8': '1'}
        prompt = kw['input_text']
        match = re.search(r'BCC_WRITE_OK:([0-9a-f]{32})', prompt)
        assert match
        nonce = match.group(1)
        f = r / ('codex_write_probe_' + nonce + '.txt')
        f.write_text('BCC_WRITE_OK:' + nonce + '\n', encoding='utf-8')
        kw['output'].write_text('sandbox: workspace-write\nCodex wrote probe\n', encoding='utf-8')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(p, 'process', fake_process)
    result = p.codex_write_probe(r)
    assert result['status'] == 'PASS'
    assert result['probe_file_removed']
    assert not list(r.glob('*probe_*.txt'))


def test_zero_exit_without_any_file_is_blocked(tmp_path, monkeypatch):
    r = _setup(monkeypatch, tmp_path)
    def fake_process(argv, **kw):
        kw['output'].write_text('sandbox: read-only\nI cannot modify files\n', encoding='utf-8')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(p, 'process', fake_process)
    with pytest.raises(p.Blocked, match='sandbox=read-only'):
        p.codex_write_probe(r)
    evidence = json.loads((r/'write_probe_failure.json').read_text(encoding='utf-8'))
    assert evidence['status'] == 'BLOCKED'
    assert evidence['sandbox_read_only_in_log'] is True


def test_write_probe_nonzero_preserves_log_for_diagnosis(tmp_path, monkeypatch):
    r = _setup(monkeypatch, tmp_path)
    def fake_process(argv, **kw):
        kw['output'].write_text('ERROR: access denied by sandbox\n', encoding='utf-8')
        return SimpleNamespace(returncode=12)
    monkeypatch.setattr(p, 'process', fake_process)
    with pytest.raises(p.Blocked, match='access denied'):
        p.codex_write_probe(r)
    assert (r/'codex_write_probe.log').is_file()


def test_no_changes_logs_actual_agent_explanation(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    logfile = tmp_path/'agent.log'
    logfile.write_text('sandbox: read-only\nOnly a proposal was made; no files modified.\n', encoding='utf-8')
    ex = p.no_changes_diagnostic(logfile)
    assert isinstance(ex, p.Blocked)
    assert 'read-only' in str(ex)
    assert (p.WORK/'CODEX_NO_CHANGES_LAST.log').is_file()


def test_select_existing_api_auth_explicitly_and_without_secret(tmp_path, monkeypatch):
    r = _setup(monkeypatch, tmp_path)
    calls = []
    def check(prefix, **kw):
        calls.append((prefix,kw['env']))
        return {'login_ok':True,'exit':0,'message':'Login via API'}
    monkeypatch.setattr(p, 'codex_auth_probe', check)
    result = p.use_existing_api()
    assert result['credentials_reused'] and result['model_inference'] == 'NOT_PERFORMED'
    assert calls == [(['node','codex.js'],{'PYTHONUTF8':'1'})]
    assert json.loads((p.WORK/'codex_auth_mode.json').read_text('utf-8')) == {'mode':'api'}


def test_unavailable_existing_api_cannot_enable_billing(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(p, 'codex_auth_probe',
                        lambda prefix, **kw: {'login_ok':False,'message':'No credentials'})
    with pytest.raises(p.Blocked, match='не найден'):
        p.use_existing_api()
    assert not (p.WORK/'codex_auth_mode.json').exists()


def test_new_bat_contains_no_api_key_and_explicit_route(tmp_path):
    bat = (Path(__file__).resolve().parents[1]/'AI_USE_EXISTING_API_WINDOWS.bat').read_text('utf-8')
    assert 'use-existing-api' in bat
    assert 'sk-' not in bat
    assert 'api-login' not in bat


def test_write_probe_end_to_end_with_actual_subprocess(tmp_path, monkeypatch):
    """Real subprocess, stdin, cwd, file write and disk proof; no API used."""
    import sys
    r = _setup(monkeypatch, tmp_path)
    fake_cli = tmp_path / 'fake_codex_cli.py'
    fake_cli.write_text('''\
import re
import sys
from pathlib import Path
prompt = sys.stdin.read()
m = re.search(r"BCC_WRITE_OK:([0-9a-f]{32})", prompt)
assert m is not None
nonce = m.group(1)
path = Path('.ai_work') / 'run_0001' / ('codex_write_probe_' + nonce + '.txt')
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text('BCC_WRITE_OK:' + nonce + '\\n', encoding='utf-8')
print('sandbox: workspace-write', flush=True)
''', encoding='utf-8')
    monkeypatch.setattr(p, 'codex_backend',
                        lambda: ('native', [sys.executable, str(fake_cli)], []))
    result = p.codex_write_probe(r)
    assert result['status'] == 'PASS'
    assert 'workspace-write' in (r/'codex_write_probe.log').read_text(encoding='utf-8')
    assert not list(r.glob('*probe_*.txt'))


def test_codex_write_command_explicit_config_does_not_escalate_scope():
    cmd = p.codex_write_command(['codex'])
    assert cmd == ['codex', 'exec', '--sandbox', 'workspace-write',
                   '-c', 'sandbox_mode="workspace-write"', '-']
    assert '--dangerously-bypass-approvals-and-sandbox' not in cmd


def test_run_codex_read_only_zero_exit_is_blocked(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    def fake_process(argv, **kw):
        assert argv == p.codex_write_command(['node', 'codex.js'])
        kw['output'].write_text('sandbox: read-only\npatch rejected: writing is blocked by read-only sandbox\n', encoding='utf-8')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(p, 'process', fake_process)
    with pytest.raises(p.Blocked, match='sandbox=read-only'):
        p.run_codex('edit code', log=tmp_path / 'codex.log', timeout=10)
