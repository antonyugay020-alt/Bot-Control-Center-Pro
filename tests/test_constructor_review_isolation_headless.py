"""Headless structural invariant; real behavior is tested by the Qt module."""
import ast
from pathlib import Path


def test_preview_implementation_never_writes_effective_change_review():
    source = Path(__file__).resolve().parents[1] / 'bcc' / 'wizard.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    wizard = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'BotWizard')
    grid = next(n for n in wizard.body if isinstance(n, ast.FunctionDef) and n.name == 'update_grid_preview')
    status_writes = []
    for node in ast.walk(grid):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'setText':
            owner = node.func.value
            if isinstance(owner, ast.Attribute) and isinstance(owner.value, ast.Name) and owner.value.id == 'self':
                if owner.attr == 'review':
                    raise AssertionError('Grid preview must never overwrite the effective change review')
                if owner.attr == 'preview_status':
                    status_writes.append(node)
    assert len(status_writes) >= 2  # both empty and populated preview states
