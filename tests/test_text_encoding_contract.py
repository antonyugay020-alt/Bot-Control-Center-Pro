"""Owned UTF-8 files/logs must work under CP1251 without changing OS locale."""
import ast
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from bcc.runtime_paths import python_environment
from bcc.storage import atomic_json, load_json
from tools.qa_locale import default_text_encoding


def test_default_locale_does_not_decode_or_rewrite_utf8_report_data(tmp_path):
    payload = {'name': 'Индикатор → Әліпби / 日本語 / 🧪',
               'status': 'Работает', 'section': 'SHORT 70→60'}
    path = tmp_path / 'Проверка.json'
    atomic_json(path, payload)
    before = path.read_bytes()
    with default_text_encoding('cp1251'):
        assert io.text_encoding(None) == 'cp1251'
        with pytest.raises(UnicodeDecodeError):
            before.decode(io.text_encoding(None))
        assert load_json(path) == payload
        assert json.loads(path.read_text(encoding='utf-8')) == payload
        assert path.read_bytes() == before


def test_all_owned_path_text_boundaries_declare_their_encoding():
    root = Path(__file__).resolve().parents[1]
    missing = []
    for folder in ('bcc', 'tools', 'tests'):
        for path in (root / folder).rglob('*.py'):
            if '__pycache__' in path.parts:
                continue
            for node in ast.walk(ast.parse(path.read_bytes())):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                name = node.func.attr
                if name not in ('read_text', 'write_text'):
                    continue
                index = 0 if name == 'read_text' else 1
                declared = (node.args[index] if len(node.args) > index else
                            next((kw.value for kw in node.keywords if kw.arg == 'encoding'), None))
                if declared is None or isinstance(declared, ast.Constant) and declared.value is None:
                    missing.append(f'{path.relative_to(root)}:{node.lineno}')
    assert not missing, 'Implicit text encoding: ' + ', '.join(missing)


def test_redirected_python_logs_use_utf8_without_setting_file_or_system_locale(tmp_path):
    env = python_environment({**os.environ, 'PYTHONIOENCODING': 'cp1251'})
    assert env['PYTHONIOENCODING'] == 'utf-8'
    assert env.get('PYTHONUTF8') == os.environ.get('PYTHONUTF8')
    text = 'Индикатор — Әліпби — 日本語'
    result = subprocess.run([sys.executable, '-c', 'import sys; print(sys.argv[1])', text],
                            env=env, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.decode('utf-8').strip() == text


def test_process_namespace_fields_remain_exact_with_non_utf8_kernel_name(tmp_path):
    from bcc.process_os import _entry_local_pid
    entry = tmp_path / 'proc'
    (entry / 'ns').mkdir(parents=True)
    (entry / 'ns/pid').write_bytes(b'namespace')
    namespace = (entry / 'ns/pid').stat().st_ino
    (entry / 'status').write_bytes(b'Name:\t\xff\xfe\nNSpid:\t98532\t42\n')
    assert _entry_local_pid(entry, namespace) == 42
    assert _entry_local_pid(entry, namespace + 1) is None
