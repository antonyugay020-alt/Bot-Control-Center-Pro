"""Fail-closed Codex + local Ollama developer workflow for BCC (Python stdlib).

Requires Codex CLI, Ollama, Git on user's Windows machine. Never claims a release
or runs production trading; changes are isolated in a Git branch and assessed
from a freshly sealed source snapshot.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import getpass
import json
import os
import re
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import http.client
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / '.ai_work'
REVIEW_MAX_BYTES = 100_000
SENSITIVE_PREFIXES = ('MY_BOTS/', 'TEST_DATA/', 'Bot Reports/', '.env', 'credentials',
                      'paper_accounts.sqlite3', 'client_secret.json')


class Blocked(RuntimeError):
    pass


def safe_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    tmp.replace(path)


def process(argv, *, cwd=None, timeout=120, output: Path | None = None,
            input_text: str | None = None, env=None):
    """Run a resolved executable without shell=True and record actual failures.

    Passing a prompt via UTF-8 stdin avoids CMD quoting of arbitrary text,
    Windows command-line length limits and accidental prompt disclosure in logs.
    """
    argv = [str(x) for x in argv]
    child_input = ({'stdin': subprocess.DEVNULL} if input_text is None
                   else {'input': input_text})
    try:
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open('w', encoding='utf-8') as stream:
                return subprocess.run(argv, cwd=cwd if cwd is not None else ROOT,
                                      stdout=stream, stderr=subprocess.STDOUT,
                                      timeout=timeout, check=False, text=True, env=env,
                                      encoding='utf-8', errors='replace',
                                      **child_input)
        return subprocess.run(argv, cwd=cwd if cwd is not None else ROOT,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=timeout, check=False, text=True, env=env,
                              encoding='utf-8', errors='replace', **child_input)
    except (OSError, subprocess.TimeoutExpired) as exc:
        # The previous 'codex: FileNotFoundError' hid the executable/path that
        # failed. Here the exact OS error is saved without printing any prompt.
        diagnostic = f'{argv[0]}: {type(exc).__name__}: {exc}'
        if output:
            with output.open('a', encoding='utf-8') as stream:
                stream.write('\n' + diagnostic + '\n')
        raise Blocked(diagnostic[:800]) from exc


def codex_invocation(*args, windows: bool | None = None):
    """Resolve npm's codex.cmd on Windows, then launch through cmd.exe.

    shutil.which('codex') can succeed because PATHEXT searches .CMD, while
    CreateProcess('codex', ...) searches an executable and returns WinError 2.
    A CMD wrapper needs explicit cmd.exe /d /s /c; no model text is passed as
    a shell argument. Only fixed CLI switches are permitted here.
    """
    resolved = shutil.which('codex')
    if not resolved:
        raise Blocked('Codex CLI отсутствует в PATH (codex.cmd / codex.exe).')
    if windows is None:
        windows = os.name == 'nt'
    args = tuple(str(arg) for arg in args)
    if windows and Path(resolved).suffix.lower() in ('.cmd', '.bat'):
        # CMD expands %, ^, ! etc. Reject unusual unsafe install paths instead
        # of constructing a command with surprising shell interpretation.
        if any(c in resolved for c in ('"', '%', '^', '!', '\n', '\r')):
            raise Blocked('Нестандартный путь Codex CMD: перенесите установку в простой путь')
        if any(arg not in {'exec', '--sandbox', 'workspace-write', '-c', 'sandbox_mode="workspace-write"', '-', '--version'}
               for arg in args):
            raise Blocked('Неподдерживаемые аргументы CMD-запуска Codex')
        cmd = os.environ.get('COMSPEC') or shutil.which('cmd.exe')
        if not cmd:
            raise Blocked('Windows cmd.exe не найден (COMSPEC)')
        # With /s, CMD removes the outer double quotes, preserving quotes
        # around the absolute .cmd path (which may contain spaces).
        command_line = '""' + resolved + '"' + (' ' + ' '.join(args) if args else '') + '"'
        return [cmd, '/d', '/s', '/c', command_line]
    if windows and Path(resolved).suffix.lower() == '.ps1':
        raise Blocked('Codex найден только как PowerShell-скрипт. Нужен codex.cmd или codex.exe')
    return [resolved, *args]



def _redact_cli_output(value: str, limit=2000) -> str:
    """Keep useful Codex/Node diagnostics while hiding likely credentials."""
    value = re.sub(r"(?i)\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,})\b",
                   '<REDACTED_TOKEN>', str(value))
    value = re.sub(r"(?i)((?:api[_-]?key|authorization|password|secret|access[_-]?token)\s*[:=]\s*)([^\s,;}]+)",
                   r'\1<REDACTED>', value)
    value = re.sub(r"(?i)Bearer\s+[^\s,;]+", 'Bearer <REDACTED>', value)
    return value[:limit]


def _codex_backends(*, windows: bool | None = None):
    """Discover trustworthy native / npm launch paths without guessing PATH.

    npm on Windows creates a .cmd trampoline to a bundled JS entrypoint. Some
    machines cannot invoke that trampoline from Python even when `where codex`
    succeeds. Prefer invoking @openai/codex/bin/codex.js with node.exe directly
    if present; keep the .cmd shim as a separate fallback and diagnose both.
    """
    resolved = shutil.which('codex')
    if not resolved:
        raise Blocked('Codex CLI не найден: проверьте PATH / npm global install')
    if windows is None:
        windows = os.name == 'nt'
    path = Path(resolved)
    backends = []
    if windows and path.suffix.lower() in ('.cmd', '.bat'):
        node = shutil.which('node.exe') or shutil.which('node')
        # npm -g Windows prefix: <prefix>\codex.cmd and
        # <prefix>\node_modules\@openai\codex\bin\codex.js
        # Unix-like npm prefix on Windows also supported via lib/node_modules.
        module_roots = (
            path.parent / 'node_modules' / '@openai' / 'codex',
            path.parent / 'lib' / 'node_modules' / '@openai' / 'codex',
            path.parent.parent / 'lib' / 'node_modules' / '@openai' / 'codex',
        )
        if node:
            for module in module_roots:
                script = module / 'bin' / 'codex.js'
                package = module / 'package.json'
                if not script.is_file() or not package.is_file():
                    continue
                try:
                    name = json.loads(package.read_text(encoding='utf-8')).get('name')
                except (OSError, ValueError, UnicodeError):
                    continue
                if name == '@openai/codex':
                    backends.append(('node-direct', [str(node), str(script)]))
                    break
        try:
            wrapped = codex_invocation(windows=True)
            backends.append(('npm-cmd', wrapped))
        except Blocked as exc:
            if not backends:
                raise
        return backends
    if windows and path.suffix.lower() == '.ps1':
        raise Blocked('Только codex.ps1: требуется установленный Codex CLI .cmd/.exe')
    return [('native', [str(path)])]


def codex_backend(*, timeout=20, windows: bool | None = None):
    """Return a backend that actually executed Codex --version successfully.

    No developer command is run if preflight fails. Diagnostics always contain
    stderr/stdout of every attempted backend (redacted) and the selected path.
    """
    WORK.mkdir(parents=True, exist_ok=True)
    log = WORK / 'CODEX_PREFLIGHT_LAST.log'
    attempts = []
    try:
        backends = _codex_backends(windows=windows)
    except Blocked as exc:
        log.write_text('Codex discovery: ' + _redact_cli_output(str(exc)) + '\n', encoding='utf-8')
        raise
    for name, prefix in backends:
        try:
            result = process([*prefix, '--version'], timeout=timeout)
            rc = result.returncode
            output = _redact_cli_output(getattr(result, 'stdout', '') or '')
        except Blocked as exc:
            rc = 'EXCEPTION'
            output = _redact_cli_output(str(exc))
        attempts.append({'method': name, 'executable': prefix[0],
                         'exit': rc, 'output': output})
        if rc == 0:
            safe_json(log.with_suffix('.json'), {'selected': name, 'attempts': attempts})
            log.write_text('\n'.join(f"{a['method']}: exit={a['exit']}\n{a['output']}" for a in attempts) + '\n',
                           encoding='utf-8')
            return name, prefix, attempts
    safe_json(log.with_suffix('.json'), {'selected': None, 'attempts': attempts})
    log.write_text('\n'.join(f"{a['method']}: exit={a['exit']}\n{a['output']}" for a in attempts) + '\n',
                   encoding='utf-8')
    last = attempts[-1] if attempts else {'output': 'Codex install not found'}
    raise Blocked('codex --version: ни один способ запуска не работает. '
                  'Подробности: .ai_work\\CODEX_PREFLIGHT_LAST.log; '
                  + _redact_cli_output(last['output'], 800))



def _auth_mode():
    """Billing route is explicitly selected; never silently spend API credits."""
    state_file = WORK / 'codex_auth_mode.json'
    if not state_file.exists():
        return 'chatgpt'
    try:
        record = json.loads(state_file.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise Blocked('Файл выбора авторизации Codex повреждён; исправьте его вручную') from exc
    if not isinstance(record, dict) or record.get('mode') not in {'chatgpt', 'api'}:
        raise Blocked('Неизвестный режим оплаты Codex. Изменения исходников заблокированы')
    return record['mode']


def _api_home():
    """Store billing credentials outside source/Git and away from ChatGPT login."""
    base = os.environ.get('LOCALAPPDATA')
    return (Path(base) if base else Path.home() / '.local' / 'share') / 'BCC_Codex_API'


def _codex_environment(mode=None):
    mode = mode or _auth_mode()
    env = os.environ.copy()
    # An inherited key is NOT permission to charge the user's API account.
    env.pop('OPENAI_API_KEY', None)
    if mode == 'api':
        env['CODEX_HOME'] = str(_api_home())
    return env


def _one_time_api_key_path():
    """Local bootstrap secret, intentionally excluded from Git and source seal.

    This path may only be shipped in a user-specific private archive. It is
    consumed and deleted after successful Codex login. Never print its bytes.
    """
    return WORK / '.codex_api_key_once'


def api_login(*, key_reader=None, confirmer=None):
    """Opt into isolated API auth; never persist a key in source or Git.

    A private one-time bootstrap token is permitted only when the user explicitly
    supplies it for this particular archive and launches setup/cycle. Otherwise
    the normal separate billing consent and hidden key input are mandatory.
    """
    print('OpenAI API оплачивается отдельно от ChatGPT. Лимит $5 этот '
          'скрипт НЕ гарантирует; настройте ограничения в OpenAI Platform.', flush=True)
    bootstrap = _one_time_api_key_path()
    automatic = bootstrap.exists()
    if automatic:
        if bootstrap.is_symlink() or not bootstrap.is_file():
            raise Blocked('Неверный путь однократного ключа API')
        secret = bootstrap.read_text(encoding='utf-8').strip()
    else:
        ask = confirmer or input
        response = ask('Для включения API введите API: ').strip()
        # Users sometimes paste the key into the consent field. Never echo,
        # store, or log that input; accept it only as an explicit API opt-in.
        if response.startswith('sk-') and len(response) > 20:
            secret = response
        elif response == 'API':
            reader = key_reader or getpass.getpass
            secret = reader('Введите OpenAI API key (ввод скрыт): ').strip()
        else:
            raise Blocked('Платный режим API не включён: подтверждение не получено')
    if not secret.startswith('sk-') or len(secret) < 20 or '\n' in secret or '\r' in secret:
        raise Blocked('Неверный формат ключа. Режим авторизации не изменён')
    name, prefix, _ = codex_backend()
    api_dir = _api_home()
    api_dir.mkdir(parents=True, exist_ok=True)
    env = _codex_environment('api')
    try:
        result = process([*prefix, 'login', '--with-api-key'],
                         input_text=secret + '\n', timeout=90, env=env)
    finally:
        secret = None
    if result.returncode:
        raise Blocked('Codex API login не выполнен: ' +
                      _redact_cli_output(result.stdout or '', 500))
    probe = codex_auth_probe(prefix, env=env)
    if not probe['login_ok']:
        raise Blocked('Codex API login status не подтвердил авторизацию: ' + probe['message'])
    if automatic:
        try:
            bootstrap.unlink()
        except OSError as exc:
            raise Blocked('API вошёл, но файл временного ключа не удалось удалить: '
                          + type(exc).__name__) from exc
    WORK.mkdir(parents=True, exist_ok=True)
    safe_json(WORK / 'codex_auth_mode.json', {'mode': 'api'})
    return {'mode': 'api', 'authenticated': True, 'backend': name,
            'api_charge': 'NOT_PERFORMED', 'api_limit': 'USER_MANAGED_EXTERNALLY',
            'credentials_location': str(api_dir), 'one_time_secret_consumed': automatic}


def use_existing_api():
    """Explicitly reuse previously configured local API auth, without a key file.

    The user must invoke AI_USE_EXISTING_API_WINDOWS.bat; merely finding API
    credentials never changes billing mode. This probe does not call a model.
    """
    name, prefix, _ = codex_backend()
    probe = codex_auth_probe(prefix, env=_codex_environment('api'))
    if not probe['login_ok']:
        raise Blocked('Существующий вход OpenAI API не найден: '
                      + probe['message'][:500] + '. Используйте AI_API_LOGIN_WINDOWS.bat')
    WORK.mkdir(parents=True, exist_ok=True)
    safe_json(WORK / 'codex_auth_mode.json', {'mode': 'api'})
    return {'mode': 'api', 'backend': name, 'authenticated': True,
            'billing_note': 'API оплачивается отдельно; лимит задаётся пользователем',
            'credentials_reused': True, 'model_inference': 'NOT_PERFORMED'}


def use_chatgpt():
    """Return to ChatGPT auth without touching the isolated API credentials."""
    WORK.mkdir(parents=True, exist_ok=True)
    safe_json(WORK / 'codex_auth_mode.json', {'mode': 'chatgpt'})
    return {'mode': 'chatgpt', 'api_auto_fallback': False,
            'note': 'Лимит ChatGPT может сохраняться до даты сброса'}


def codex_auth_probe(prefix, *, timeout=25, env=None):
    """Check Codex credentials without triggering a paid model inference.

    --version proves only that Node can launch the CLI.  `login status`
    verifies whether credentials are available before changing Git state.
    API-key mode is reported separately because merely having a key in
    the environment does not prove that the key is valid.
    """
    try:
        result = process([*prefix, 'login', 'status'], timeout=timeout, env=env)
        return {'login_ok': result.returncode == 0,
                'exit': result.returncode,
                'message': _redact_cli_output(getattr(result, 'stdout', '') or '', 1200)}
    except Blocked as exc:
        return {'login_ok': False, 'exit': 'EXCEPTION',
                'message': _redact_cli_output(str(exc), 1200)}


def _safe_codex_failure_tail(log: Path, *, max_bytes=16000, display_limit=1400) -> str:
    """Extract only a bounded, redacted tail for an actionable console error."""
    try:
        with log.open('rb') as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - max_bytes))
            text = fh.read().decode('utf-8', errors='replace')
    except OSError as exc:
        return 'Невозможно прочитать codex.log: ' + type(exc).__name__
    text = text.strip().replace('\x00', '')
    if len(text) > display_limit:
        text = '...\n' + text[-display_limit:]
    return _redact_cli_output(text, display_limit) or 'codex.log пуст (CLI завершился без сообщений)'


def _save_codex_exec_diagnostic(*, log: Path, backend: str, exit_code: int) -> str:
    """Persist the real failure near AI_CYCLE_WINDOWS_LAST.log; no retries."""
    tail = _safe_codex_failure_tail(log)
    diag = ('Codex exec failed; backend=' + backend + ', exit=' + str(exit_code)
            + '\nSource log: ' + str(log) + '\nLog tail (redacted):\n' + tail
            + '\nDo not rerun the cycle before checking Git status and this log.\n')
    target = WORK / 'CODEX_EXEC_FAILURE_LAST.log'
    try:
        WORK.mkdir(parents=True, exist_ok=True)
        target.write_text(diag, encoding='utf-8')
    except OSError:
        pass
    return tail


def ensure_command(cmd):
    if not shutil.which(cmd):
        raise Blocked(f'Не найдена команда: {cmd}. Установите её на Windows и повторите doctor.')


def git(*args, timeout=45):
    ensure_command('git')
    r = process(['git', *args], timeout=timeout)
    if r.returncode:
        raise Blocked('Git: ' + r.stdout.strip()[:600])
    return r.stdout.strip()


def repo_check():
    if git('rev-parse', '--show-toplevel') != str(ROOT):
        # On Windows Git may output POSIX-normalized path; compare resolved paths.
        if Path(git('rev-parse', '--show-toplevel')).resolve() != ROOT.resolve():
            raise Blocked('BCC должен быть отдельным Git-репозиторием')
    if git('status', '--porcelain', '--untracked-files=normal'):
        raise Blocked('Рабочее дерево Git изменено. Зафиксируйте свои изменения перед новым AI-циклом.')
    head = git('rev-parse', '--verify', 'HEAD')
    return head


def local_request(url, endpoint, *, payload=None, timeout=6):
    parsed_url = urllib.parse.urlsplit(url)
    if (parsed_url.scheme != 'http' or parsed_url.hostname not in {'127.0.0.1', 'localhost'}
            or parsed_url.username or parsed_url.password or parsed_url.path not in ('', '/')
            or parsed_url.query or parsed_url.fragment):
        raise Blocked('Разрешён только локальный Ollama (127.0.0.1) без перенаправления')
    try:
        connection = http.client.HTTPConnection(parsed_url.hostname, parsed_url.port or 11434,
                                                timeout=timeout)
        try:
            body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
            connection.request('GET' if body is None else 'POST', endpoint, body=body,
                               headers={'Content-Type': 'application/json'})
            response = connection.getresponse()
            if response.status != 200:
                raise Blocked('Ollama HTTP status ' + str(response.status))
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise Blocked('Ollama: слишком большой ответ')
            return json.loads(raw)
        finally:
            connection.close()
    except (OSError, ValueError, TypeError) as exc:
        raise Blocked('Локальный Ollama недоступен или вернул некорректный ответ') from exc


def local_ollama(url, *, timeout=6):
    parsed = local_request(url, '/api/tags', timeout=timeout)
    names = [x.get('name', '') for x in parsed.get('models', []) if isinstance(x, dict)]
    return names


def ollama_review(*, url, model, diff, mission, timeout=480):
    if len(diff.encode('utf-8')) > REVIEW_MAX_BYTES:
        raise Blocked('Diff слишком большой для полного независимого ревью: разбейте работу по архитектурным границам')
    names = local_ollama(url)
    if model not in names:
        raise Blocked(f'Ollama: модель {model!r} не установлена; доступны {names[:15]}')
    payload = {
        'model': model, 'stream': False, 'format': 'json',
        'options': {'temperature': 0, 'num_ctx': 16384},
        'prompt': ('Ты независимый инженер по надёжности Windows/Python/Qt торговых PAPER-ботов. '
                   'Ниже задание и полный Git diff. Найди архитектурные разрывы, состояния гонок, '
                   'потерю данных, повторные сделки, ненадёжный STOP, обход управляющего слоя. '
                   'Не считай прежние тесты доказательством успеха. Ответь строго JSON: '
                   '{"blocking_findings": [{"file":"...","issue":"...","evidence":"..."}], '
                   '"nonblocking_findings": [], "review_complete": true}. '
                   'Если данных недостаточно для ревью, укажи review_complete=false. '
                   'Никаких изменений файлов.\nMISSION:\n' + mission + '\nDIFF:\n' + diff),
    }
    try:
        raw = local_request(url, '/api/generate', payload=payload, timeout=timeout)
        review = json.loads(raw['response'])
    except (Blocked, OSError, ValueError, KeyError) as exc:
        raise Blocked('Ollama review: недоступен или вернул некорректный JSON') from exc
    if not isinstance(review, dict) or type(review.get('review_complete')) is not bool:
        raise Blocked('Ollama review: нет достоверного признака полноты')
    for key in ('blocking_findings', 'nonblocking_findings'):
        if not isinstance(review.get(key), list):
            raise Blocked('Ollama review: отсутствует корректный список ' + key)
    if not review['review_complete']:
        raise Blocked('Ollama review: модель не смогла проверить полный diff')
    return review


def changed_paths(base):
    raw = process(['git','diff','--name-only','-z', base, '--'], timeout=30)
    if raw.returncode:
        raise Blocked('Невозможно получить Git diff')
    return [x.replace('\\', '/') for x in raw.stdout.split('\x00') if x]


def validate_paths(paths):
    for path in paths:
        normalized = path.casefold()
        if (path.startswith('/') or '..' in Path(path).parts or
                any(normalized.startswith(p.casefold()) for p in SENSITIVE_PREFIXES) or
                normalized.endswith(('.sqlite', '.sqlite3', '.db', '.key', '.pem'))):
            raise Blocked('Обнаружено изменение закрытых данных / PAPER-счетов: ' + path)


def diff_from(base):
    # Include new source files, but first check untracked names for sensitive paths.
    untracked = process(['git', 'ls-files', '--others', '--exclude-standard', '-z'], timeout=30)
    if untracked.returncode:
        raise Blocked('Невозможно проверить новые файлы Git')
    validate_paths([x.replace('\\', '/') for x in untracked.stdout.split('\x00') if x])
    added = process(['git', 'add', '-N', '--', '.'], timeout=30)
    if added.returncode:
        raise Blocked('Git не смог учесть новые исходные файлы')
    changed = changed_paths(base)
    if not changed:
        raise Blocked('Codex не изменил исходники; нельзя засчитывать пустой цикл')
    validate_paths(changed)
    # --no-ext-diff keeps the text deterministic and avoids user diff driver.
    r = process(['git','diff','--no-ext-diff','--no-color',base,'--'], timeout=45)
    if r.returncode:
        raise Blocked('git diff завершился неуспешно')
    if 'Binary files ' in r.stdout and ' differ' in r.stdout:
        raise Blocked('Бинарные изменения нельзя одобрить только текстовым Ollama review')
    return r.stdout, changed


def doctor(model, url):
    mode = _auth_mode()
    result = {'codex_auth_mode': mode, 'platform': platform.platform(), 'python': sys.version.split()[0],
              'gpu_note': 'RTX 3080 Ti проверяется локальной системой пользователя; удалённый доступ не предполагается',
              'commands': {name: bool(shutil.which(name)) for name in ('git','codex','ollama')},
              'ollama_model': model, 'ollama_ok': False, 'windows': os.name == 'nt'}
    try:
        result['ollama_models'] = local_ollama(url)
        result['ollama_ok'] = model in result['ollama_models']
    except (OSError, Blocked, ValueError) as exc:
        result['ollama_error'] = type(exc).__name__ + ': ' + str(exc)
    # A PATH lookup is not proof of launchability. Probe both the npm shim and
    # Node.js direct entrypoint, persist actual stdout/stderr for diagnosis.
    result['codex_launch_ok'] = False
    result['codex_login_ok'] = False
    result['codex_api_key_present'] = bool(os.environ.get('OPENAI_API_KEY'))
    if result['commands']['codex']:
        try:
            backend_name, prefix, attempts = codex_backend()
            result['codex_launch_ok'] = True
            result['codex_backend'] = backend_name
            result['codex_version'] = attempts[-1]['output'].strip()[:200]
            auth = (codex_auth_probe(prefix, env=_codex_environment('api'))
                    if mode == 'api' else codex_auth_probe(prefix))
            result['codex_login_ok'] = auth['login_ok']
            result['codex_login_exit'] = auth['exit']
            result['codex_login_message'] = auth['message']
        except Blocked as exc:
            result['codex_launch_error'] = str(exc)
            result['codex_diagnostic_log'] = str(WORK / 'CODEX_PREFLIGHT_LAST.log')
    result['ready_for_ai_cycle'] = (all(result['commands'].values())
                                   and result['ollama_ok'] and result['codex_launch_ok']
                                   and result['codex_login_ok'])
    if result['codex_launch_ok'] and not result['codex_login_ok']:
        result['codex_auth_note'] = (
            'API-режим не авторизован. Запустите AI_API_LOGIN_WINDOWS.bat.' if mode == 'api'
            else 'Вход через ChatGPT не подтверждён. Выполните codex login. '
                 'Для платного API существует отдельный AI_API_LOGIN_WINDOWS.bat.')
    return result


def _git_output(*args):
    """Query Git without treating an absent optional value as a fatal error."""
    r = process(['git', *args], timeout=45)
    return r.stdout.strip() if r.returncode == 0 else None


def ensure_local_git_identity():
    """Set a non-personal fallback only when no effective Git identity exists.

    Never change global Git configuration or impersonate the Windows account.
    """
    for field, fallback in (('user.name', 'BCC Local Automation'),
                            ('user.email', 'bcc-automation@localhost.invalid')):
        if not _git_output('config', '--get', field):
            git('config', '--local', field, fallback)
    # A local fallback is limited to this BCC source repository.
    for field in ('user.name', 'user.email'):
        if not _git_output('config', '--get', field):
            raise Blocked('Git не удалось настроить автора локального коммита: ' + field)


def init_repo():
    """Create or safely resume a *sealed* initial Git commit.

    A first run may fail after `git init` / `git add` but before the first
    commit (e.g. Git author identity missing). A mere .git existence check
    must never label that state initialized. No hard reset, clean, checkout,
    or deletion of user files is performed.
    """
    ensure_command('git')
    from tools.verify_package import verify
    # Verify *before* touching a new or half-initialized repository. This
    # prevents blessing source files edited since their sealed manifest.
    verify(ROOT)
    manifest = json.loads((ROOT / 'SHA256_MANIFEST.json').read_text(encoding='utf-8'))
    source = sorted(manifest['files']) + ['SHA256_MANIFEST.json']
    validate_paths(source)
    is_new = not (ROOT / '.git').exists()
    if is_new:
        git('init')
    # The repository must be rooted at this BCC folder, never a parent repo.
    real_root = _git_output('rev-parse', '--show-toplevel')
    if real_root is None or Path(real_root).resolve() != ROOT.resolve():
        raise Blocked('Неверный Git-репозиторий для BCC: ' + str(real_root))
    head = _git_output('rev-parse', '--verify', 'HEAD')
    if head is not None:
        # An existing initialized repo is accepted only if clean. It must not
        # be silently overwritten, rebased or reset after a prior Codex run.
        repo_check()
        return {'initialized': False, 'root': str(ROOT), 'head': head,
                'note': 'Существующий чистый Git-репозиторий'}

    # An unborn HEAD is recoverable, but only when the staged index contains
    # no files outside the sealed source allowlist and does not differ from
    # the verified working copies. This protects pre-existing user staging.
    existing = _git_output('ls-files', '--cached', '-z')
    if existing is None:
        raise Blocked('Git не смог прочитать индекс незавершённой инициализации')
    staged = {name.replace('\\', '/') for name in existing.split('\x00') if name}
    unexpected = staged - set(source)
    if unexpected:
        raise Blocked('В Git index присутствуют посторонние файлы; не изменяю их: '
                      + ', '.join(sorted(unexpected)[:8]))
    tracked_mismatch = process(['git', 'diff', '--quiet', '--'], timeout=45)
    if tracked_mismatch.returncode != 0:
        raise Blocked('В Git index есть версия файла, отличная от проверенных исходников; '
                      'исправление вручную, без автоматического reset')
    other = _git_output('ls-files', '--others', '--exclude-standard', '-z')
    if other is None:
        raise Blocked('Git не смог проверить новые файлы')
    unrelated = {name.replace('\\', '/') for name in other.split('\x00') if name} - set(source)
    if unrelated:
        raise Blocked('Перед первым коммитом обнаружены неучтённые файлы: '
                      + ', '.join(sorted(unrelated)[:8]))

    ensure_local_git_identity()
    # Files already added before a failed commit are harmless; stage only the
    # explicit verified manifest paths. Git metadata and PAPER files stay out.
    for start in range(0, len(source), 40):
        git('add', '--', *source[start:start + 40])
    git('commit', '-m', 'BCC: sealed baseline before AI development')
    verify(ROOT)
    repo_check()
    return {'initialized': True, 'recovered': not is_new,
            'root': str(ROOT), 'head': git('rev-parse', 'HEAD')}


def codex_write_command(prefix):
    """Pin the requested write scope using both documented CLI settings.

    Some Codex installations load a user/global config with a read-only
    sandbox. Supply an explicit CLI config value in addition to --sandbox.
    This cannot override an enforced administrator/host policy; the on-disk
    write proof remains the only authority for proceeding.
    """
    return [*prefix, 'exec', '--sandbox', 'workspace-write',
            '-c', 'sandbox_mode="workspace-write"', '-']


def _read_only_rejection(log: Path) -> bool:
    """Detect observed refusal, never interpret a zero exit as write access."""
    tail = _safe_codex_failure_tail(log, display_limit=6000)
    return bool(re.search(r'(?mi)^sandbox:\s*read-only\s*$', tail)
                or re.search(r'(?i)writing is blocked by read-only sandbox', tail))


def codex_write_block_reason(log: Path) -> str:
    return ('Codex фактически запущен в sandbox=read-only, несмотря на явные '
            'параметры workspace-write. Это может быть политика Codex/Windows '
            'или настройки установленного Codex. Программа НЕ должна обходить '
            'принудительные ограничения. Проверьте локальные настройки Codex '
            'и разрешите workspace-write для доверенной копии BCC; не используйте '
            'danger-full-access или обход подтверждений. Журнал: ' + str(log))


def run_codex(prompt, *, log, timeout):
    # Use the exact backend that passed --version instead of switching silently
    # from Node to the potentially broken Windows .cmd trampoline.
    name, prefix, attempts = codex_backend()
    argv = codex_write_command(prefix)
    r = process(argv, timeout=timeout, output=log, input_text=prompt,
                env=_codex_environment())
    if _read_only_rejection(log):
        raise Blocked(codex_write_block_reason(log))
    if r.returncode:
        # Never retry: Codex might have already edited files before failing.
        tail = _save_codex_exec_diagnostic(log=log, backend=name, exit_code=r.returncode)
        if 'usage limit' in tail.casefold() or 'you.ve hit your usage limit' in tail.casefold():
            raise Blocked('Лимит Codex через ChatGPT исчерпан. '
                          'Повторный запуск не поможет: дождитесь сброса лимита '
                          'или явно выберите платный API через AI_API_LOGIN_WINDOWS.bat. '
                          'Исходники и Git-ветка сохранены. Подробности: ' + str(log))
        raise Blocked(f'Codex ({name}) завершился с кодом {r.returncode}. '
                      f'Последние строки codex.log: {tail} '
                      f'Полный журнал: {log}; диагностика: {WORK / "CODEX_EXEC_FAILURE_LAST.log"}')


def codex_write_probe(run_dir: Path, *, timeout: int = 180):
    """Prove an actual agent can write to this workspace before a long mission.

    `codex --version` and `codex login status` do NOT establish write rights.
    This deliberately invokes one *small* Codex model request; it may incur API
    usage. The result is checked on disk, not inferred from exit=0 or model text.
    The probe file is inside ignored .ai_work, never source or PAPER state.
    """
    run_dir = Path(run_dir).resolve()
    if not run_dir.is_relative_to(WORK.resolve()) or run_dir == WORK.resolve():
        raise Blocked('Неверная папка проверки прав Codex')
    run_dir.mkdir(parents=True, exist_ok=True)
    nonce = uuid.uuid4().hex
    target = run_dir / ('codex_write_probe_' + nonce + '.txt')
    if target.exists():
        raise Blocked('Файл проверки Codex неожиданно существует')
    relative = target.relative_to(ROOT.resolve()).as_posix()
    marker = 'BCC_WRITE_OK:' + nonce + '\n'
    prompt = (
        'Проверка прав записи рабочего пространства BCC. Это НЕ торговая задача. '
        'Создай только один файл с относительным путём ' + relative + ' и точным '
        'UTF-8 содержимым (с переводом строки): ' + marker.strip() + '. '
        'Используй доступный инструмент изменения файлов. '
        'Не изменяй другие файлы, не читай PAPER/ключи, не создавай сделки. '
        'Заверши выполнение после записи.\n'
    )
    name, prefix, _ = codex_backend()
    log = run_dir / 'codex_write_probe.log'
    result = process(codex_write_command(prefix),
                     input_text=prompt, output=log, timeout=timeout,
                     env=_codex_environment())
    tail = _safe_codex_failure_tail(log, display_limit=1200)
    if _read_only_rejection(log):
        safe_json(run_dir / 'write_probe_failure.json',
                  {'status': 'BLOCKED', 'log': str(log), 'file_written': target.exists(),
                   'sandbox_read_only_in_log': True})
        raise Blocked(codex_write_block_reason(log))
    if result.returncode:
        raise Blocked('Codex не выполнил проверку прав записи (exit='
                      + str(result.returncode) + '). ' + tail
                      + '; журнал: ' + str(log))
    try:
        actual = target.read_text(encoding='utf-8') if target.is_file() and not target.is_symlink() else None
    except (OSError, UnicodeError):
        actual = None
    if actual is None or actual.strip() != marker.strip():
        explanation = ('Codex завершился с кодом 0, но НЕ создал точный проверочный файл. '
                       'Возможны sandbox=read-only, отсутствие права редактирования или '
                       'отказ агента использовать инструменты. Реальное изменение исходников '
                       'НЕ подтверждено. ')
        safe_json(run_dir / 'write_probe_failure.json',
                  {'status': 'BLOCKED', 'log': str(log), 'file_written': target.exists(),
                   'sandbox_read_only_in_log': bool(re.search(r'(?mi)^sandbox:\s*read-only\s*$', tail))})
        raise Blocked(explanation + 'Последние строки codex.log: ' + tail
                      + '; файл: ' + str(log))
    target.unlink()
    # Any changes outside the ignored probe directory are not permitted.
    if git('status', '--porcelain', '--untracked-files=normal'):
        raise Blocked('Codex изменил исходники во время проверки прав записи; '
                      'автоматическое удаление запрещено, проверьте Git diff')
    return {'status': 'PASS', 'backend': name, 'model_call': True,
            'source_changes': 'NOT_ATTEMPTED', 'probe_file_removed': True}


def no_changes_diagnostic(log: Path):
    """Explain a successful Codex exit with zero source diff, without a false PASS."""
    tail = _safe_codex_failure_tail(log, display_limit=2200)
    reason = ('Codex явно запущен в read-only sandbox. '
              if re.search(r'(?mi)^sandbox:\s*read-only\s*$', tail) else
              'Codex не внёс изменений исходников, хотя проверка записи прошла. ')
    message = (reason + 'Это не завершённая разработка. Посмотрите вывод Codex: '
               + tail + '; полный журнал: ' + str(log))
    (WORK / 'CODEX_NO_CHANGES_LAST.log').write_text(_redact_cli_output(message, 3000),
                                                     encoding='utf-8')
    return Blocked(message)


def seal_and_qa(run_dir, qa, *, base=None):
    if qa == 'windows' and os.name != 'nt':
        raise Blocked('Нативную Windows Qt E2E нельзя запускать на Linux')
    from tools.seal_source import seal, source_paths
    from tools.verify_package import verify
    if base is not None:
        source_list = {name for name, _ in source_paths(ROOT)} | {'SHA256_MANIFEST.json'}
        changed = changed_paths(base)
        missing = set(changed) - source_list
        if missing:
            raise Blocked('Изменения не входят в манифест исходников: ' + repr(sorted(missing)[:12]))
    checkpoint = 'BCC_AI_REVIEW_' + run_dir.name.replace('-', '_')
    archive = run_dir / (checkpoint + '.zip')
    seal(ROOT, archive, checkpoint=checkpoint)
    with zipfile.ZipFile(archive) as z:
        # Fail closed on traversal and unexpected roots.
        for member in z.namelist():
            if (member.startswith('/') or '..' in Path(member).parts or
                    not member.startswith(checkpoint + '/')):
                raise Blocked('Небезопасный ZIP path')
        z.extractall(run_dir / 'unpacked')
    clean = run_dir / 'unpacked' / checkpoint
    integrity = verify(clean)
    if qa == 'none':
        return {'source': integrity, 'qa': 'NOT_RUN', 'release_ready': False}
    if qa == 'windows':
        if os.name != 'nt':
            raise Blocked('Нативную Windows Qt E2E нельзя запускать на Linux')
        cmd = [sys.executable, '-m', 'tools.windows_check', '--automated-only']
        timeout = 7200
    elif qa == 'headless':
        if os.name == 'nt':
            raise Blocked('На Windows используйте полный native QA, не headless-исключения')
        # Headless-only diagnostic. Not a native release acceptance.
        cmd = [sys.executable, '-m', 'pytest', '-q', '--disable-warnings',
               '--ignore=tests/test_wizard_visual_qt.py', 'tests']
        timeout = 7200
    else:
        raise Blocked('Неизвестный режим QA')
    r = process(cmd, cwd=clean, timeout=timeout, output=run_dir / 'qa.log')
    return {'source': integrity, 'qa': 'PASS' if r.returncode == 0 else 'FAIL',
            'qa_exit': r.returncode, 'qa_mode': qa, 'release_ready': False,
            'manual_windows_paper': 'NOT_CONFIRMED'}


def cycle(args):
    if not args.mission.is_file():
        raise Blocked('Не найден файл заданий: ' + str(args.mission))
    mission = args.mission.read_text(encoding='utf-8')
    if len(mission) < 100:
        raise Blocked('Не заполнены требования и критерии приёмки BCC')
    d = doctor(args.model, args.url)
    if not d['ready_for_ai_cycle']:
        missing = [name for name, installed in d['commands'].items() if not installed]
        details = ('Отсутствуют команды: ' + ', '.join(missing) + '. ') if missing else ''
        if not d['ollama_ok']:
            details += ('Ollama должен работать на http://127.0.0.1:11434; '
                        'нужна модель ' + args.model + '. ')
        if d.get('codex_launch_ok') and not d.get('codex_login_ok') and not d.get('codex_api_key_present'):
            details += ('Codex запускается, но не подтверждает авторизацию. '
                        'Откройте командную строку, выполните codex login и войдите через ChatGPT. '
                        'Диагностика: ' + str(d.get('codex_login_message', '')) + '. ')
        raise Blocked(details + 'Проверка готовности не пройдена; исходники не изменены.')
    print('[BCC AI] Checking clean Git baseline...', flush=True)
    base = repo_check()
    run_id = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:6]
    branch = 'ai/bcc-' + run_id
    WORK.mkdir(exist_ok=True)
    lock_path = WORK / 'cycle.lock'
    try:
        lockfd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise Blocked('Другой цикл выполняется или остался lock; проверьте процессы перед удалением lock') from exc
    with os.fdopen(lockfd, 'w') as lock:
        lock.write(str(os.getpid()) + '\n' + branch + '\n')
    run_dir = WORK / run_id
    run_dir.mkdir()
    state = {'kind': 'BCC_AI_DEVELOPMENT_INTERNAL_NOT_RELEASE', 'branch': branch, 'base': base,
             'codex': 'NOT_RUN', 'ollama': 'NOT_RUN', 'qa': 'NOT_RUN',
             'release_ready': False, 'run_dir': str(run_dir)}
    safe_json(run_dir / 'status.json', state)
    try:
        git('switch', '-c', branch)
        task_prompt = ('Работай непосредственно в исходниках BCC в текущем Git branch. '
                       'Не делай мелкие изолированные патчи: сначала сверь архитектуру с требованиями, '
                       'затем исправь общие причины и интегрируй весь заявленный маршрут. '
                       'НЕ меняй MY_BOTS, TEST_DATA, PAPER SQLite, реальные настройки или API ключи. '
                       'Не запускай live сделки. Не создавай релиз. '
                       'Запускай доступные тесты и оставь подробные изменения в исходниках.\n\n' + mission)
        print('[BCC AI] Checking real Codex workspace-write capability...', flush=True)
        state['write_probe'] = codex_write_probe(run_dir)
        safe_json(run_dir / 'status.json', state)
        print('[BCC AI] Write probe PASS. Starting the integrated BCC source task...', flush=True)
        print('[BCC AI] Codex working. Live log: ' + str(run_dir / 'codex.log'), flush=True)
        run_codex(task_prompt + '\n\nОБЯЗАТЕЛЬНЫЙ РЕЗУЛЬТАТ: внеси реальные изменения '
                  'в исходники и тесты, а не только анализ. Если технически нельзя '
                  'изменить файлы, укажи точный блокер и не называй работу завершённой.',
                  log=run_dir / 'codex.log', timeout=args.codex_timeout)
        state['codex'] = 'DONE'
        try:
            diff, paths = diff_from(base)
        except Blocked as exc:
            if 'не изменил исходники' in str(exc):
                raise no_changes_diagnostic(run_dir / 'codex.log') from exc
            raise
        (run_dir / 'changes.diff').write_text(diff, encoding='utf-8')
        state['changed_files'] = paths
        for pass_id in (1, 2):
            print(f'[BCC AI] Ollama independent review, round {pass_id}/2...', flush=True)
            review = ollama_review(url=args.url, model=args.model, diff=diff, mission=mission)
            safe_json(run_dir / f'ollama_review_{pass_id}.json', review)
            if not review['blocking_findings']:
                state['ollama'] = 'REVIEW_NO_BLOCKERS_NOT_A_CERTIFICATION'
                break
            if pass_id == 2:
                state['ollama'] = 'BLOCKERS_REMAIN'
                raise Blocked('Независимое ревью содержит блокирующие замечания после повторной интеграции')
            fix_prompt = ('Независимое ревью выявило системные риски. '
                          'Устрани общие причины вместе, не меняя реальные PAPER-счета. '
                          'После исправления повторно проверь связанные сценарии.\n'
                          + json.dumps(review['blocking_findings'], ensure_ascii=False))
            run_codex(fix_prompt, log=run_dir / 'codex_review_fix.log', timeout=args.codex_timeout)
            diff, paths = diff_from(base)
            (run_dir / 'changes.diff').write_text(diff, encoding='utf-8')
            state['changed_files'] = paths
        print('[BCC AI] Sealing clean source snapshot and running Windows QA...', flush=True)
        state.update(seal_and_qa(run_dir, args.qa, base=base))
        return state
    except Exception as exc:
        state['error'] = type(exc).__name__ + ': ' + str(exc)
        raise
    finally:
        safe_json(run_dir / 'status.json', state)
        # Intentionally leave branch and source changes intact for human audit.
        lock_path.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description='BCC local Codex + Ollama, fail-closed')
    parser.add_argument('action', choices=('doctor', 'init', 'cycle', 'api-login', 'use-existing-api', 'use-chatgpt'))
    parser.add_argument('--model', default=os.getenv('BCC_OLLAMA_MODEL', 'qwen3:8b'))
    parser.add_argument('--url', default=os.getenv('BCC_OLLAMA_URL', 'http://127.0.0.1:11434'))
    parser.add_argument('--mission', type=Path, default=ROOT / 'BCC_AI_MISSION_RU.md')
    parser.add_argument('--qa', choices=('none', 'headless', 'windows'), default='windows')
    parser.add_argument('--codex-timeout', type=int, default=3600)
    args = parser.parse_args(argv)
    try:
        if args.action == 'doctor':
            result = doctor(args.model, args.url)
        elif args.action == 'api-login':
            result = api_login()
        elif args.action == 'use-chatgpt':
            result = use_chatgpt()
        elif args.action == 'use-existing-api':
            result = use_existing_api()
        elif args.action == 'init':
            result = init_repo()
        else:
            result = cycle(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.action == 'doctor' and not result['ready_for_ai_cycle']:
            return 2
        # A failed or incomplete QA must never appear successful to the BAT
        # launcher even when the developer cycle itself ran to completion.
        if args.action == 'cycle' and result.get('qa') != 'PASS' and args.qa != 'none':
            return 3
        return 0
    except (Blocked, ValueError, OSError) as exc:
        print(json.dumps({'status': 'BLOCKED', 'reason': str(exc)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
