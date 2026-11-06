"""End-to-end cold invocation of the actual Windows acceptance entrypoint.

The previous suite never exercised the `python tools/windows_check.py` import
path used by the Windows launcher. This regression exercises that path in a
fresh, sealed copy, including in isolated Python mode and from another cwd.
"""
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1]


@pytest.fixture
def sealed_bundle(tmp_path):
    root = tmp_path / "Проверка Windows с пробелами" / "Extracted"
    root.mkdir(parents=True)
    source_manifest = SOURCE / "SHA256_MANIFEST.json"
    manifest = json.loads(source_manifest.read_text(encoding="utf-8"))
    for name in manifest["files"]:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SOURCE / name, target)
    shutil.copyfile(source_manifest, root / source_manifest.name)
    other = tmp_path / "unrelated cwd"
    other.mkdir()
    return root, other


def invoke_preflight(root, other, *, isolated):
    return subprocess.run(
        [sys.executable, *(["-I"] if isolated else []),
         str(root / "tools" / "windows_check.py"), "--preflight-only"],
        cwd=other,
        env={key: val for key, val in os.environ.items() if key != "PYTHONPATH"},
        capture_output=True,
        timeout=30,
    )


@pytest.mark.parametrize("isolated", [False, True])
def test_real_direct_script_preflight_no_tools_import_error(sealed_bundle, isolated):
    root, other = sealed_bundle
    result = invoke_preflight(root, other, isolated=isolated)
    assert result.returncode == 0, result.stdout + result.stderr
    assert b"No module named 'tools'" not in result.stdout + result.stderr
    assert not (root / ".check_venv").exists()
    archives = list(root.glob("BCC_VALIDATION_RESULTS_*.zip"))
    assert len(archives) == 1
    with zipfile.ZipFile(archives[0]) as report:
        summary = json.loads(report.read(next(x for x in report.namelist()
                                               if x.endswith("check_summary.json"))))
    assert summary["status"] == "SOURCE_INTEGRITY_PASS_ONLY"
    assert summary["source_integrity"]["status"] == "PASS"
    assert summary["steps"] == []
    assert summary["release_ready"] is False
    assert summary["manual_acceptance"] == "NOT_CONFIRMED"


def test_real_direct_script_rejects_modified_source(sealed_bundle):
    root, other = sealed_bundle
    with (root / "tools" / "verify_windows.py").open("ab") as handle:
        handle.write(b"\n# corrupted before launch\n")
    result = invoke_preflight(root, other, isolated=True)
    assert result.returncode != 0
    archives = list(root.glob("BCC_VALIDATION_RESULTS_*.zip"))
    assert len(archives) == 1
    with zipfile.ZipFile(archives[0]) as report:
        summary = json.loads(report.read(next(x for x in report.namelist()
                                               if x.endswith("check_summary.json"))))
    assert summary["status"] == "ERROR"
    assert summary["release_ready"] is False
    assert "SHA-256" in summary["error"]
    assert not (root / ".check_venv").exists()


def test_actual_windows_launcher_uses_direct_entrypoint():
    launcher = (SOURCE / "tools" / "start_windows_check.ps1").read_text(encoding="utf-8")
    assert "tools\\windows_check.py" in launcher
    source = (SOURCE / "tools" / "windows_check.py").read_text(encoding="utf-8")
    assert 'sys.path.insert(0, str(ROOT))' in source

@pytest.mark.parametrize("failed_step", ["environment", "dependencies", "automated"])
def test_windows_state_machine_never_opens_paper_after_failed_step(
    tmp_path, monkeypatch, failed_step
):
    """Run real main() through its Windows branch with mocked side effects.

    This does not claim native Windows pass; it only validates fail-closed order.
    """
    import types
    from tools import windows_check

    monkeypatch.setattr(windows_check, "ROOT", tmp_path)
    monkeypatch.setattr(
        windows_check, "os",
        types.SimpleNamespace(name="nt", environ={}),
    )
    monkeypatch.setattr(
        windows_check, "verify_source_snapshot",
        lambda: {"status": "PASS", "verified_files": 127},
    )
    visited = []

    def fake_run_step(name, command, output, **kwargs):
        visited.append(name)
        assert name != "application", "PAPER app started after failed automation"
        return {"step": name, "exit_code": 5 if name == failed_step else 0}

    monkeypatch.setattr(windows_check, "run_step", fake_run_step)
    assert windows_check.main([]) != 0
    assert visited[-1] == failed_step
    assert "application" not in visited
    archive = next(tmp_path.glob("BCC_VALIDATION_RESULTS_*.zip"))
    with zipfile.ZipFile(archive) as z:
        name = next(x for x in z.namelist() if x.endswith("check_summary.json"))
        summary = json.loads(z.read(name))
    assert summary["release_ready"] is False
    assert summary["manual_acceptance"] == "NOT_CONFIRMED"
