"""End-to-end authority invariants for stop/restart command revisions."""
import pytest
from bcc.coordination import Coordinator


def test_stop_start_never_reauthorizes_old_worker(tmp_path):
    registry = Coordinator(tmp_path / "coordination.sqlite")
    registry.initialize()
    registry.register("VELES/bot-a")
    registry.set_desired("VELES/bot-a", "RUNNING")
    first = registry.get_registration("VELES/bot-a")["command_version"]
    assert registry.execution_authorized("VELES/bot-a", first)
    registry.set_desired("VELES/bot-a", "STOPPED")
    assert not registry.execution_authorized("VELES/bot-a", first)
    registry.set_desired("VELES/bot-a", "RUNNING")
    second = registry.get_registration("VELES/bot-a")["command_version"]
    assert second > first
    assert not registry.execution_authorized("VELES/bot-a", first)
    assert registry.execution_authorized("VELES/bot-a", second)


def test_authorization_fails_closed_on_missing_registry(tmp_path):
    registry = Coordinator(tmp_path / "coordination.sqlite")
    registry.initialize()
    registry.register("GRID/a")
    assert not registry.execution_authorized("GRID/a", 0)
    assert not registry.execution_authorized("MISSING/a", 0)
    with pytest.raises((OSError, Exception)):
        (tmp_path / "coordination.sqlite").unlink()
        registry.execution_authorized("GRID/a", 0)
