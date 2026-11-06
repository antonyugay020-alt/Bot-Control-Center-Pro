"""Native Qt modules can never silently disappear from Windows QA."""
import json

import pytest

from tools import run_partitioned_tests as qa


def test_qa_inventory_fails_if_qt_modules_are_silently_omitted(tmp_path, monkeypatch):
    root = tmp_path / 'package'
    tests = root / 'tests'
    tests.mkdir(parents=True)
    (tests / 'test_core.py').write_text('def test_ok(): pass', encoding="utf-8")
    (tests / 'test_qt.py').write_text('def test_qt(): pass', encoding="utf-8")
    inv = tmp_path / 'inventory.json'
    inv.write_text(json.dumps({'schema': 1, 'cases': [['tests.test_core', 'test_ok']]}), encoding="utf-8")
    monkeypatch.setattr(qa, 'ROOT', root)
    with pytest.raises(ValueError, match='inventory differs'):
        qa.partitions(inv)
    assert qa.partitions(inv, excluded={'test_qt.py'}) == [['test_core.py']]
    with pytest.raises(ValueError, match='Unknown excluded'):
        qa.partitions(inv, excluded={'test_missing.py'})
