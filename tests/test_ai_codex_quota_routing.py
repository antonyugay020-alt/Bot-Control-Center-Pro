"""Quota routing and billing consent are deterministic/offline (no API charges)."""
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from tools import ai_dev_pipeline as p


def test_default_mode_never_uses_environment_api_key(tmp_path, monkeypatch):
    monkeypatch.setattr(p, 'WORK', tmp_path)
    monkeypatch.setenv('OPENAI_API_KEY', 'sk-dont-spend-in-chatgpt-mode-AAAAAAAA')
    assert p._auth_mode() == 'chatgpt'
    env = p._codex_environment()
    assert 'OPENAI_API_KEY' not in env


def test_corrupt_auth_mode_is_blocking(tmp_path, monkeypatch):
    monkeypatch.setattr(p, 'WORK', tmp_path)
    (tmp_path/'codex_auth_mode.json').write_text('{bad', encoding='utf-8')
    with pytest.raises(p.Blocked):
        p._auth_mode()


def test_api_login_requires_active_confirmation(tmp_path, monkeypatch):
    monkeypatch.setattr(p, 'WORK', tmp_path)
    monkeypatch.setattr(p, 'codex_backend', lambda: pytest.fail('must not launch CLI'))
    with pytest.raises(p.Blocked, match='подтверждение'):
        p.api_login(confirmer=lambda prompt: '', key_reader=lambda _: 'sk-secret')
    assert not (tmp_path/'codex_auth_mode.json').exists()


def test_api_login_key_only_on_stdin_and_separate_home(tmp_path, monkeypatch):
    monkeypatch.setattr(p, 'WORK', tmp_path / '.ai_work')
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    monkeypatch.setattr(p, 'codex_backend', lambda: ('node-direct',['node','codex.js'],[]))
    calls = []
    secret = 'sk-topsecretAAAABBBBCCCCDDDD'
    def fake_process(argv, **kw):
        calls.append((argv,kw))
        assert argv == ['node','codex.js','login','--with-api-key']
        assert kw['input_text'] == secret+'\n'
        assert kw['env']['CODEX_HOME'] == str(tmp_path/'local'/'BCC_Codex_API')
        assert 'OPENAI_API_KEY' not in kw['env']
        return SimpleNamespace(returncode=0,stdout='Logged in')
    monkeypatch.setattr(p, 'process', fake_process)
    monkeypatch.setattr(p, 'codex_auth_probe', lambda prefix,env=None: {'login_ok':True,'message':'ok'})
    result = p.api_login(confirmer=lambda _: 'API', key_reader=lambda _: secret)
    assert result['mode'] == 'api'
    assert result['api_charge'] == 'NOT_PERFORMED'
    assert len(calls)==1
    saved = (p.WORK/'codex_auth_mode.json').read_text(encoding='utf-8')
    assert secret not in saved and json.loads(saved)['mode']=='api'


def test_api_login_failure_does_not_switch_billing(tmp_path, monkeypatch):
    monkeypatch.setattr(p, 'WORK', tmp_path / '.ai_work')
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path/'local'))
    monkeypatch.setattr(p, 'codex_backend', lambda: ('native',['codex'],[]))
    monkeypatch.setattr(p, 'process', lambda argv, **kw: SimpleNamespace(returncode=1, stdout='invalid API key'))
    with pytest.raises(p.Blocked, match='не выполнен'):
        p.api_login(confirmer=lambda _: 'API', key_reader=lambda _: 'sk-aaaaaaaaaaaaaaaaaaaaaaaaaaa')
    assert not (p.WORK/'codex_auth_mode.json').exists()


def test_switch_back_never_erases_api_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(p, 'WORK', tmp_path/'.ai_work')
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path/'local'))
    home=p._api_home()
    home.mkdir(parents=True)
    (home/'auth.json').write_text('private',encoding='utf-8')
    p.WORK.mkdir(parents=True)
    p.safe_json(p.WORK/'codex_auth_mode.json', {'mode':'api'})
    assert p.use_chatgpt()['mode']=='chatgpt'
    assert p._auth_mode()=='chatgpt'
    assert (home/'auth.json').read_text(encoding='utf-8') == 'private'


def test_quota_failure_is_not_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(p, 'WORK', tmp_path/'.ai_work')
    monkeypatch.setattr(p,'codex_backend',lambda: ('node-direct',['node','codex.js'],[]))
    calls=[]
    def fake_process(argv,**kw):
        calls.append(argv)
        kw['output'].parent.mkdir(parents=True,exist_ok=True)
        kw['output'].write_text("ERROR: You've hit your usage limit. try again at Nov 11th, 2026 11:45 AM.",encoding='utf-8')
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(p,'process',fake_process)
    log=tmp_path/'.ai_work'/'test'/'codex.log'
    with pytest.raises(p.Blocked,match='Лимит Codex'):
        p.run_codex('some BCC mission',log=log,timeout=10)
    assert len(calls)==1
    assert (p.WORK/'CODEX_EXEC_FAILURE_LAST.log').exists()


def test_windows_explicit_auth_entrypoints_are_not_auto_billing():
    root=Path(__file__).resolve().parents[1]
    api=(root/'AI_API_LOGIN_WINDOWS.bat').read_text(encoding='utf-8')
    chat=(root/'AI_USE_CHATGPT_WINDOWS.bat').read_text(encoding='utf-8')
    assert '-m tools.ai_dev_pipeline api-login' in api
    assert '-m tools.ai_dev_pipeline use-chatgpt' in chat
    assert 'OPENAI_API_KEY' not in api+chat
