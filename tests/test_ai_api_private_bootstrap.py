"""Test one-time user-provided API bootstrap without network or paid inference."""
import json
from pathlib import Path
import os
from types import SimpleNamespace
from tools import ai_dev_pipeline as p, windows_ai_launcher as w


def test_one_time_secret_is_stdin_only_and_removed_after_success(tmp_path, monkeypatch):
    monkeypatch.setattr(p, 'WORK', tmp_path/'.ai_work')
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path/'local'))
    p.WORK.mkdir()
    bootstrap=p._one_time_api_key_path()
    secret='sk-test-private-AAAAABBBBBCCCCCDDDDD'
    bootstrap.write_text(secret,encoding='utf-8')
    monkeypatch.setattr(p,'codex_backend',lambda:('node-direct',['node','codex.js'],[]))
    calls=[]
    def fake_run(argv,**kw):
        assert argv==['node','codex.js','login','--with-api-key']
        assert kw['input_text']==secret+'\n'
        assert 'OPENAI_API_KEY' not in kw['env']
        calls.append(True)
        return SimpleNamespace(returncode=0,stdout='Login success')
    monkeypatch.setattr(p,'process',fake_run)
    monkeypatch.setattr(p,'codex_auth_probe',lambda *a,**kw:{'login_ok':True,'message':'ok'})
    result=p.api_login(confirmer=lambda _: (_ for _ in ()).throw(AssertionError('no prompt')))
    assert result['one_time_secret_consumed'] is True
    assert len(calls)==1
    assert not bootstrap.exists()
    assert secret not in (p.WORK/'codex_auth_mode.json').read_text(encoding='utf-8')
    assert json.loads((p.WORK/'codex_auth_mode.json').read_text(encoding='utf-8'))['mode']=='api'


def test_one_time_secret_retained_after_login_failure(tmp_path,monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path/'local'))
    monkeypatch.setattr(p,'WORK',tmp_path/'.ai_work')
    p.WORK.mkdir()
    bootstrap=p._one_time_api_key_path()
    bootstrap.write_text('sk-test-private-AAAAABBBBBCCCCCDDDDD',encoding='utf-8')
    monkeypatch.setattr(p,'codex_backend',lambda:('native',['codex'],[]))
    monkeypatch.setattr(p,'process',lambda *a,**kw:SimpleNamespace(returncode=1,stdout='unauthorized'))
    try:
        p.api_login()
    except p.Blocked as exc:
        assert 'не выполнен' in str(exc)
    else:
        raise AssertionError('failure should block')
    assert bootstrap.exists()
    assert not (p.WORK/'codex_auth_mode.json').exists()


def test_pasted_key_in_consent_field_is_not_logged(tmp_path,monkeypatch):
    monkeypatch.setattr(p,'WORK',tmp_path/'.ai_work')
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path/'local'))
    secret='sk-test-private-AAAAABBBBBCCCCCDDDDD'
    monkeypatch.setattr(p,'codex_backend',lambda:('node-direct',['node','codex.js'],[]))
    monkeypatch.setattr(p,'process',lambda argv,**kw:SimpleNamespace(returncode=0,stdout='ok'))
    monkeypatch.setattr(p,'codex_auth_probe',lambda *a,**kw:{'login_ok':True,'message':'ok'})
    result=p.api_login(confirmer=lambda _:secret)
    assert result['authenticated']
    assert secret not in (p.WORK/'codex_auth_mode.json').read_text(encoding='utf-8')


def test_launch_auto_bootstraps_before_doctor(tmp_path,monkeypatch):
    monkeypatch.setattr(w,'ROOT',tmp_path)
    monkeypatch.setattr(w,'WORK',tmp_path/'.ai_work')
    w.WORK.mkdir()
    (w.WORK/'.codex_api_key_once').write_text('sk-private-test',encoding='utf-8')
    monkeypatch.setattr(w,'os',SimpleNamespace(name='nt',getenv=os.getenv))
    actions=[]
    def fake_step(action,log,**kw):
        actions.append(action)
        return 0
    monkeypatch.setattr(w,'run_step',fake_step)
    assert w.main(['setup'])==0
    assert actions==['api-login','doctor','init']
