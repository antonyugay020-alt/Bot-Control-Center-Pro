"""Native QA must not inherit arbitrary globally-installed pytest plugins."""
from types import SimpleNamespace

from tools import verify_windows


def test_tests_subprocess_disables_external_pytest_plugins(tmp_path, monkeypatch):
    evidence = {}
    def fake_run(command, **kwargs):
        evidence.update(command=command, **kwargs)
        return 0
    monkeypatch.setattr(verify_windows, "run_logged_command", fake_run)
    output = tmp_path / "out"
    output.mkdir()
    result = verify_windows.run_step("tests", ["-m", "pytest", "tests"], output, 30)
    assert result["status"] == "PASS"
    assert evidence["env"]["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert evidence["env"]["BCC_TEST_ARTIFACTS"] == str(output / "process_checks")
    assert evidence["timeout"] == 30


def test_non_test_steps_do_not_leak_instrumentation_override(tmp_path, monkeypatch):
    evidence = {}
    def fake_run(command, **kwargs):
        evidence.update(kwargs)
        return 0
    monkeypatch.setattr(verify_windows, "run_logged_command", fake_run)
    output = tmp_path / "out"
    output.mkdir()
    monkeypatch.delenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", raising=False)
    result = verify_windows.run_step("gui", ["-m", "bcc.gui_check"], output, 30)
    assert result["status"] == "PASS"
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD" not in evidence["env"]
