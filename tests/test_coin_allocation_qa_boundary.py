"""WIP source-bound Windows acceptance safety contract.

This static regression does NOT execute PowerShell, pip, public WebSocket,
trading worker or GitHub Actions; it checks the source-controlled launcher.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _check():
    script = ROOT / "tools" / "check_veles_coin_capital_windows.ps1"
    return script.read_text(encoding="utf-8")


def test_native_qa_requires_isolated_clean_source_and_no_live_accounts():
    src = _check()
    assert "BCC_VELES_COIN_QA" in src
    assert "SOURCE" in src
    assert "codex/veles-coin-capital-policy-20261118" in src
    assert "status --porcelain" in src
    assert "MY_BOTS" in src
    assert "ReparsePoint" in src
    assert "source SHA already has an acceptance workspace" in src


def test_sealing_and_source_reopen_are_mandatory_before_windows_check():
    src = _check()
    seal = src.index("'tools.seal_source'")
    extract = src.index("Expand-Archive")
    run = src.index("-AutomatedOnly")
    assert seal < extract < run
    assert "SHA256_MANIFEST.json" in src
    assert "start_windows_check.ps1" in src
    assert "-Venue bybit" in src


def test_single_report_and_no_release_claim_when_manual_qt_missing():
    src = _check()
    assert "LATEST_REPORT.zip" in src
    assert "AUTOMATED_WINDOWS_RESULTS.zip" in src
    assert "'NOT_RUN'" in src
    assert "release_approved = $false" in src
    assert "paid_model_requests = 0" in src
    assert "github_actions_requested = 0" in src
    assert "real_orders_authorized = $false" in src


def test_runner_does_not_call_unapproved_actions_or_modify_live_bots():
    src = _check()
    for forbidden in (
        "gh workflow run", "gh api", "Invoke-RestMethod",
        "Stop-Process", "Restart-Computer", "Shutdown-Computer",
        "git merge", "git push", "git reset --hard",
    ):
        assert forbidden not in src
    assert "CLEAN_UNPACK" in src
    assert "Get-ChildItem -LiteralPath $check" in src
