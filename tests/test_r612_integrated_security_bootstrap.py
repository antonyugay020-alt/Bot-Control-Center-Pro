"""Regression tests for installation, consoleless errors and PAPER secret guards."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from bcc.config import is_secret_key, reject_private_data
from bcc.runtime_paths import python_environment

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("name", [
    "api_key", "relayer_api_key", "POLYMARKET_RELAYER_API_KEY",
    "walletPrivateKey", "client_secret", "OPENAI_API_KEY", "signing_key",
    "oauth_access_token", "authToken", "refresh_token", "SMTP_PASSWORD",
    "seed_phrase", "mnemonic", "credentials",
])
def test_namespaced_credentials_are_rejected_before_paper_export(name):
    assert is_secret_key(name)
    with pytest.raises(ValueError, match="ключи доступа"):
        reject_private_data({"parameters": {name: "private-material"}})


@pytest.mark.parametrize("name", [
    "token", "tokens", "condition", "market_token_id", "api_key_address",
    "api_key_rotation_due", "symbol", "account_id",
])
def test_public_market_metadata_not_mistaken_for_credentials(name):
    assert not is_secret_key(name)
    reject_private_data({"positions": {name: "public-id"}})


def test_install_and_start_share_private_python_environment():
    installer = (ROOT / "INSTALL.bat").read_text(encoding="utf-8").lower()
    launcher = (ROOT / "START.bat").read_text(encoding="utf-8").lower()
    for text in (installer, launcher):
        assert ".venv" in text
        assert "pyside6" in text
        assert "requirements" in installer
    assert "pythonw.exe" in launcher
    # Only the project-owned interpreter may start the desktop; the former
    # global pyw fallback could launch against a different Python environment.
    assert "if errorlevel 1 goto install" in launcher
    assert "exit /b 1" in launcher
    assert 'start "" "%pyw%"' in launcher
    assert 'start "" pyw -3' not in launcher


@pytest.mark.parametrize("action", ["nonzero", "exception"])
def test_consoleless_launch_errors_are_recorded_to_user_directory(tmp_path, action):
    test_root = tmp_path / "sealed boot"
    test_root.mkdir()
    (test_root / "launch.pyw").write_bytes((ROOT / "launch.pyw").read_bytes())
    (test_root / "app.py").write_text(
        ("def main():\n    return 7\n" if action == "nonzero" else
         "def main():\n    raise RuntimeError('simulated GUI failure')\n"),
        encoding="utf-8",
    )
    appdata = tmp_path / "local-app-data"
    env = {**os.environ, "LOCALAPPDATA": str(appdata)}
    result = subprocess.run([sys.executable, str(test_root / "launch.pyw")],
                            cwd=tmp_path, env=python_environment(env), capture_output=True,
                            text=True, encoding='utf-8', timeout=10)
    assert result.returncode != 0
    log = (appdata / "BotControlCenterPro" / "startup.log").read_text(encoding="utf-8")
    assert ("nonzero exit status: 7" if action == "nonzero"
            else "simulated GUI failure") in log
    assert (test_root / "startup.log").exists() is False


@pytest.mark.parametrize("amount", [True, False, float("inf"), float("nan"), 0])
def test_paper_capital_rejects_invalid_numeric_amounts(tmp_path, amount):
    from bcc.storage import Store
    folder = tmp_path / "MY_BOTS" / "bot"
    folder.mkdir(parents=True)
    store = Store(folder, 100)
    with pytest.raises(ValueError):
        store.adjust_capital(amount, "transaction-123")
    assert store.read(include_charts=False)["cash"] == 100
    assert not store.history()


@pytest.mark.parametrize("event_id", [None, "", " ", 12, "x" * 129])
def test_paper_capital_rejects_invalid_transaction_ids(tmp_path, event_id):
    from bcc.storage import Store
    folder = tmp_path / "MY_BOTS" / "bot"
    folder.mkdir(parents=True)
    store = Store(folder, 100)
    with pytest.raises(ValueError):
        store.adjust_capital(25, event_id)
    assert store.read(include_charts=False)["cash"] == 100
    assert not store.history()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, 0, True, "long"])
def test_hub_rejects_invalid_subscription_ttls(tmp_path, value):
    from bcc.market_hub import HubRegistry
    registry = HubRegistry(tmp_path / "hub.sqlite")
    url = "https://api.bybit.com/v5/market/orderbook"
    with pytest.raises(ValueError):
        registry.subscribe(url, {"symbol": "BTCUSDT"}, lifetime=value)
    assert not registry.has_active_subscriptions()


@pytest.mark.parametrize("param", [float("nan"), float("inf"), object()])
def test_hub_rejects_non_json_subscription_parameters(tmp_path, param):
    from bcc.market_hub import HubRegistry
    registry = HubRegistry(tmp_path / "hub.sqlite")
    url = "https://api.bybit.com/v5/market/orderbook"
    with pytest.raises(ValueError):
        registry.subscribe(url, {"symbol": "BTCUSDT", "bad": param})
    assert not registry.has_active_subscriptions()


def test_paper_account_rolls_back_secrets_added_to_diagnostics(tmp_path):
    from bcc.storage import AccountingError, Store
    folder = tmp_path / "MY_BOTS" / "bot"
    folder.mkdir(parents=True)
    store = Store(folder, 100)
    with pytest.raises(AccountingError):
        with store.transaction(include_charts=False) as (state, events):
            state["diagnostics"]["market"] = {"relayer_api_key": "sample-credential"}
    assert store.read(include_charts=False)["diagnostics"] == {}
    assert store.read(include_charts=False)["cash"] == 100


def test_both_windows_validation_entrypoints_use_one_isolated_qa_path():
    full = (ROOT / "CHECK_WINDOWS.bat").read_text(encoding="utf-8").lower()
    automated = (ROOT / "VALIDATE_WINDOWS.bat").read_text(encoding="utf-8").lower()
    ps1 = (ROOT / "tools" / "start_windows_check.ps1").read_text(encoding="utf-8").lower()
    assert "start_windows_check.ps1" in full and "start_windows_check.ps1" in automated
    assert "-automatedonly" in automated and "--automated-only" in ps1
    assert "pip install" not in automated
    assert ".check_venv" in (ROOT / "tools" / "windows_check.py").read_text(encoding="utf-8")

