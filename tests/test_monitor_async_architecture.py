"""Headless architectural invariants for Modern Qt fleet monitor.

These tests do not replace native Qt acceptance; they ensure blocking database
and process inspection cannot silently be moved back into QWidget.refresh().
"""
import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / 'bcc' / 'monitoring.py'


def _method(class_name, method_name):
    module = ast.parse(SOURCE.read_text(encoding='utf-8'))
    owner = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    return next(node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name == method_name)


def test_gui_refresh_does_not_read_sqlite_or_inspect_processes():
    for name in ('refresh', '_render', '_snapshot_received'):
        calls = [n for n in ast.walk(_method('RunningBotsWindow', name)) if isinstance(n, ast.Call)]
        for call in calls:
            assert not (isinstance(call.func, ast.Name) and call.func.id == 'snapshot_all')
            assert not (isinstance(call.func, ast.Name) and call.func.id == 'Store')
            assert not (isinstance(call.func, ast.Attribute) and call.func.attr in {'read', 'history', 'status', 'active_snapshot'})


def test_background_runner_owns_snapshot_io_and_reports_completion():
    fn = _method('_SnapshotTask', 'run')
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
    assert any(isinstance(n.func, ast.Name) and n.func.id == 'snapshot_all' for n in calls)
    assert any(isinstance(n.func, ast.Attribute) and n.func.attr == 'emit' for n in calls)


def test_monitor_does_not_start_one_worker_per_timer_tick():
    fn = _method('RunningBotsWindow', 'refresh')
    branches = [n for n in ast.walk(fn) if isinstance(n, ast.If)]
    assert any(isinstance(n.test, ast.Attribute) and n.test.attr == '_loading' for n in branches)
    assert any(isinstance(n, ast.Attribute) and n.attr == '_refresh_pending' for n in ast.walk(fn))


def test_snapshot_generation_fences_closed_window_results():
    task = _method('_SnapshotTask', 'run')
    monitor = _method('RunningBotsWindow', '_snapshot_received')
    close = _method('RunningBotsWindow', 'closeEvent')
    assert any(isinstance(n, ast.Attribute) and n.attr == 'generation' for n in ast.walk(task))
    assert any(isinstance(n, ast.Compare) and any(isinstance(v, ast.Attribute) and v.attr == '_generation' for v in ast.walk(n)) for n in ast.walk(monitor))
    assert any(isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Attribute) and n.target.attr == '_generation' for n in ast.walk(close))

def test_snapshot_failure_never_keeps_stale_running_status():
    module = ast.parse(SOURCE.read_text(encoding='utf-8'))
    fn = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == 'snapshot_all')
    handlers = [n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler)]
    assert any(isinstance(n, ast.Assign) and any(isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) and t.value.id == 'row' for t in n.targets) and isinstance(n.value, ast.Constant) and n.value.value == 'Ошибка' for h in handlers for n in ast.walk(h))


def test_whole_fleet_snapshot_failure_clears_cached_running_rows():
    fn = _method('RunningBotsWindow', '_snapshot_received')
    error_branch = next(n for n in ast.walk(fn) if isinstance(n, ast.If)
                        and isinstance(n.test, ast.Compare)
                        and any(isinstance(x, ast.Name) and x.id == 'error'
                                for x in ast.walk(n.test)))
    attrs = [n for n in ast.walk(error_branch.body[0]) if isinstance(n, ast.Attribute)]
    assert any(a.attr == '_raw_records' for a in attrs)
    assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == '_render' for node in error_branch.body
               for n in ast.walk(node))
