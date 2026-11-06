"""Codex Windows/npm .cmd launcher regressions (no live Codex or external requests)."""
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import ai_dev_pipeline as pipeline


def test_windows_npm_codex_cmd_resolves_to_explicit_cmd(monkeypatch):
    npm_shim = r'C:\Users\anubi\AppData\Roaming\npm\codex.CMD'
    monkeypatch.setattr(pipeline.shutil, 'which', lambda name: npm_shim if name == 'codex' else None)
    monkeypatch.setenv('COMSPEC', r'C:\Windows\System32\cmd.exe')
    command = pipeline.codex_invocation('exec', '--sandbox', 'workspace-write', '-', windows=True)
    assert command[:4] == [r'C:\Windows\System32\cmd.exe', '/d', '/s', '/c']
    assert command[4] == f'""{npm_shim}" exec --sandbox workspace-write -"'
    assert 'BCC_AI_MISSION_RU.md' not in ' '.join(command)


def test_windows_npm_path_with_spaces_is_quoted(monkeypatch):
    npm_shim = r'C:\Program Files\NodeJS and NPM\codex.cmd'
    monkeypatch.setattr(pipeline.shutil, 'which', lambda name: npm_shim)
    monkeypatch.setenv('COMSPEC', r'C:\Windows\System32\cmd.exe')
    command = pipeline.codex_invocation('--version', windows=True)
    assert command[4] == f'""{npm_shim}" --version"'


def test_windows_native_codex_exe_executes_without_cmd(monkeypatch):
    monkeypatch.setattr(pipeline.shutil, 'which', lambda name: r'C:\Tools\codex.exe')
    assert pipeline.codex_invocation('--version', windows=True) == [r'C:\Tools\codex.exe', '--version']


def test_windows_codex_path_with_special_expansions_is_blocked(monkeypatch):
    monkeypatch.setattr(pipeline.shutil, 'which', lambda name: r'C:\Users\%USERNAME%\npm\codex.cmd')
    monkeypatch.setenv('COMSPEC', r'C:\Windows\System32\cmd.exe')
    with pytest.raises(pipeline.Blocked, match='Нестандартный путь'):
        pipeline.codex_invocation('--version', windows=True)


def test_windows_codex_cmd_never_receives_untrusted_prompt_as_arg(monkeypatch):
    monkeypatch.setattr(pipeline.shutil, 'which', lambda name: r'C:\npm\codex.cmd')
    monkeypatch.setenv('COMSPEC', r'C:\Windows\System32\cmd.exe')
    with pytest.raises(pipeline.Blocked, match='аргументы'):
        pipeline.codex_invocation('exec', 'dangerous & task', windows=True)


def test_process_passes_multiline_prompt_via_stdin_not_command(tmp_path):
    prompt = 'Разработка BCC: проверь STOP & START | PAPER.\n' * 12000
    output = tmp_path / 'codex.log'
    command = [sys.executable, '-c', 'import sys; data=sys.stdin.read(); print(len(data), data.startswith("Разработка"))']
    result = pipeline.process(command, cwd=tmp_path, timeout=10, output=output, input_text=prompt)
    assert result.returncode == 0
    logged = output.read_text(encoding='utf-8')
    assert f'{len(prompt)} True' in logged
    assert 'Разработка BCC' not in logged


def test_codex_failed_spawn_reports_os_exception_and_writes_log(tmp_path):
    path = tmp_path / 'no-such-executable'
    log = tmp_path / 'codex.log'
    with pytest.raises(pipeline.Blocked, match='FileNotFoundError') as error:
        pipeline.process([str(path), '--version'], cwd=tmp_path, output=log)
    assert str(path) in str(error.value)
    assert 'FileNotFoundError' in log.read_text(encoding='utf-8')


def test_doctor_fails_closed_when_codex_path_exists_but_launch_fails(monkeypatch):
    monkeypatch.setattr(pipeline.shutil, 'which', lambda name: '/fake/' + name)
    monkeypatch.setattr(pipeline, 'local_ollama', lambda *args: ['qwen3:8b'])
    monkeypatch.setattr(pipeline, 'process', lambda argv, **kw: SimpleNamespace(returncode=2))
    result = pipeline.doctor('qwen3:8b', 'http://127.0.0.1:11434')
    assert all(result['commands'].values())
    assert not result['codex_launch_ok']
    assert not result['ready_for_ai_cycle']
    assert 'codex --version' in result['codex_launch_error']


def test_run_codex_checks_real_exit_code(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, 'codex_backend', lambda: ('native', [sys.executable, '-c', 'import sys; sys.exit(27)'], []))
    with pytest.raises(pipeline.Blocked, match='27'):
        pipeline.run_codex('safe prompt', log=tmp_path / 'codex.log', timeout=5)


def test_direct_npm_node_backend_actually_starts_a_cli(monkeypatch, tmp_path):
    """Run a local synthetic npm CLI through the real Node binary, without cmd.

    This exercises process creation, Unicode stdin, and option ordering instead
    of just asserting the shape of a Windows command string.
    """
    import json
    import shutil
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is not installed in this test environment')
    shim = tmp_path / 'npm prefix' / 'codex.cmd'
    shim.parent.mkdir(parents=True)
    shim.write_text('@echo off\r\nexit /b 88\r\n', encoding='utf-8')
    mod = shim.parent / 'node_modules' / '@openai' / 'codex'
    (mod / 'bin').mkdir(parents=True)
    (mod / 'package.json').write_text(json.dumps({'name': '@openai/codex'}), encoding='utf-8')
    (mod / 'bin' / 'codex.js').write_text('''
const fs=require('fs');
if(process.argv.includes('--version')){console.log('codex-cli synthetic 1.0');process.exit(0)}
if(process.argv.includes('exec')){const data=fs.readFileSync(0,'utf8');console.log('TASK:'+data.length+':'+data.includes('Проверка'))}
else process.exit(42);
''', encoding='utf-8')
    original = pipeline.shutil.which
    monkeypatch.setattr(pipeline.shutil, 'which',
                        lambda name: str(shim) if name == 'codex' else str(node) if name in ('node.exe', 'node') else original(name))
    monkeypatch.setattr(pipeline, 'WORK', tmp_path / '.ai_work')
    backends = pipeline._codex_backends(windows=True)
    assert backends[0][0] == 'node-direct'
    assert str(mod / 'bin' / 'codex.js') in backends[0][1]
    method, prefix, attempts = pipeline.codex_backend(windows=True)
    assert method == 'node-direct'
    assert attempts[-1]['exit'] == 0
    result = pipeline.process([*prefix, 'exec', '--sandbox', 'workspace-write', '-'],
                              cwd=tmp_path, input_text='Проверка БCC с UTF-8')
    assert result.returncode == 0
    assert 'TASK:' in result.stdout and ':true' in result.stdout
    assert 'node-direct' in (tmp_path / '.ai_work' / 'CODEX_PREFLIGHT_LAST.log').read_text(encoding='utf-8')


def test_codex_failed_preflight_captures_actual_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, 'WORK', tmp_path)
    monkeypatch.setattr(pipeline, '_codex_backends', lambda windows=None: [('npm-cmd', ['fake-cmd'])])
    monkeypatch.setattr(pipeline, 'process',
                        lambda argv, **kwargs: SimpleNamespace(returncode=1, stdout='Node: Cannot find module codex.js'))
    with pytest.raises(pipeline.Blocked, match='Cannot find module'):
        pipeline.codex_backend()
    diagnostic = (tmp_path / 'CODEX_PREFLIGHT_LAST.log').read_text(encoding='utf-8')
    assert 'exit=1' in diagnostic and 'Cannot find module' in diagnostic


def test_codex_fallback_selects_second_backend_without_exec_retries(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, 'WORK', tmp_path)
    monkeypatch.setattr(pipeline, '_codex_backends', lambda windows=None:
                        [('node-direct', ['broken-node']), ('npm-cmd', ['working-cmd'])])
    calls=[]
    def fake_process(argv, **kwargs):
        calls.append(argv)
        if argv[0] == 'broken-node':
            return SimpleNamespace(returncode=1, stdout='Node fatal error')
        return SimpleNamespace(returncode=0, stdout='codex-cli 1.0')
    monkeypatch.setattr(pipeline, 'process', fake_process)
    method, prefix, attempts = pipeline.codex_backend()
    assert method == 'npm-cmd'
    assert len(attempts) == 2 and all(call[-1] == '--version' for call in calls)


def test_codex_preflight_redacts_credentials(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, 'WORK', tmp_path)
    monkeypatch.setattr(pipeline, '_codex_backends', lambda windows=None: [('npm-cmd', ['fake-cmd'])])
    secret = 'sk-' + 'A'*30
    monkeypatch.setattr(pipeline, 'process', lambda argv, **kw:
                        SimpleNamespace(returncode=1, stdout='OPENAI_API_KEY='+secret))
    with pytest.raises(pipeline.Blocked):
        pipeline.codex_backend()
    log = (tmp_path / 'CODEX_PREFLIGHT_LAST.log').read_text(encoding='utf-8')
    assert secret not in log
    assert '<REDACTED>' in log


def test_codex_auth_probe_checks_login_without_model_call(monkeypatch):
    calls = []
    def fake(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=1, stdout='Not logged in')
    monkeypatch.setattr(pipeline, 'process', fake)
    result = pipeline.codex_auth_probe(['node', 'codex.js'])
    assert calls == [['node', 'codex.js', 'login', 'status']]
    assert not result['login_ok']
    assert result['message'] == 'Not logged in'


def test_doctor_never_equates_version_success_to_authorized(monkeypatch):
    monkeypatch.setattr(pipeline.shutil, 'which', lambda name: '/mock/' + name)
    monkeypatch.setattr(pipeline, 'local_ollama', lambda *args: ['qwen3:8b'])
    monkeypatch.setattr(pipeline, 'codex_backend', lambda: (
        'node-direct', ['node', 'codex.js'], [{'output': 'codex-cli 0.167.0'}]))
    monkeypatch.setattr(pipeline, 'codex_auth_probe', lambda prefix:
                        {'login_ok': False, 'exit': 1, 'message': 'Not logged in'})
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    result = pipeline.doctor('qwen3:8b', 'http://127.0.0.1:11434')
    assert result['codex_launch_ok']
    assert not result['codex_login_ok']
    assert not result['ready_for_ai_cycle']
    assert 'codex login' in result['codex_auth_note']


def test_doctor_does_not_spend_inherited_api_key_implicitly(monkeypatch):
    monkeypatch.setattr(pipeline.shutil, 'which', lambda name: '/mock/' + name)
    monkeypatch.setattr(pipeline, 'local_ollama', lambda *args: ['qwen3:8b'])
    monkeypatch.setattr(pipeline, 'codex_backend', lambda: (
        'native', ['/mock/codex'], [{'output': 'codex-cli 0.167.0'}]))
    monkeypatch.setattr(pipeline, 'codex_auth_probe', lambda prefix:
                        {'login_ok': False, 'exit': 1, 'message': 'No credentials'})
    monkeypatch.setenv('OPENAI_API_KEY', 'this-is-only-a-test-string')
    result = pipeline.doctor('qwen3:8b', 'http://127.0.0.1:11434')
    assert not result['ready_for_ai_cycle']
    assert result['codex_auth_mode'] == 'chatgpt'
    assert 'this-is-only-a-test-string' not in str(result)


def test_run_codex_nonzero_preserves_and_reports_real_error(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, 'WORK', tmp_path / '.ai_work')
    monkeypatch.setattr(pipeline, 'codex_backend', lambda: (
        'node-direct', [sys.executable, '-c',
                        'import sys;print("Not logged in. Run codex login");sys.exit(1)'], []))
    logfile = tmp_path / '20261106' / 'codex.log'
    with pytest.raises(pipeline.Blocked, match='Not logged in') as error:
        pipeline.run_codex('private development task', log=logfile, timeout=10)
    assert logfile.exists()
    assert 'Not logged in' in logfile.read_text(encoding='utf-8')
    diagnostic = pipeline.WORK / 'CODEX_EXEC_FAILURE_LAST.log'
    assert diagnostic.exists()
    assert 'Not logged in' in diagnostic.read_text(encoding='utf-8')
    assert 'private development task' not in diagnostic.read_text(encoding='utf-8')
    assert 'CODEX_EXEC_FAILURE_LAST.log' in str(error.value)


def test_exec_failure_summary_redacts_secret_and_is_bounded(tmp_path):
    logfile = tmp_path / 'codex.log'
    secret = 'sk-' + 'A' * 35
    logfile.write_text('noise\n' * 10000 + 'Authorization: Bearer ' + secret
                       + '\nFATAL: sandbox denied\n', encoding='utf-8')
    tail = pipeline._safe_codex_failure_tail(logfile)
    assert 'sandbox denied' in tail
    assert secret not in tail
    assert 'REDACTED' in tail
    assert len(tail) <= 1400 + 10
