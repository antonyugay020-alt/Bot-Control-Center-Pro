"""Bybit WS must be an enforced capability for new PAPER workers.

Never restart/replace a running old Hub or silently trade on REST snapshots.
"""
import json
from pathlib import Path

import pytest

from bcc import hub_supervisor


def test_new_bybit_worker_requests_managed_quote_and_orderbook_streams():
    source = (Path(__file__).resolve().parents[1] / "bcc" / "lifecycle.py").read_text(encoding="utf-8")
    assert "ensure_hub(self.repo.root, bybit_ws=True)" in source
    assert 'env["BCC_BYBIT_WS_BOOKS"] = "1"' in source
    assert 'env["BCC_BYBIT_WS_QUOTES"] = "1"' in source


def test_live_legacy_hub_is_never_restarted_or_reused_as_ws_producer(tmp_path, monkeypatch):
    root = tmp_path / "qa"
    root.mkdir()
    (root / "market_hub_process.json").write_text(
        json.dumps({"pid": 1234, "created": 100.0, "leader_owner": "1234:lease"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(hub_supervisor, "_matches", lambda record, cache: True)
    def forbidden_spawn(*args, **kwargs):
        raise AssertionError("Existing live Hub must not be replaced")
    monkeypatch.setattr(hub_supervisor, "_spawn_hub", forbidden_spawn)
    with pytest.raises(hub_supervisor.HubUnavailable, match="no Bybit orderbook WebSocket"):
        hub_supervisor.ensure_hub(root, bybit_ws=True)


def test_legacy_hub_record_does_not_fake_ws_capability(tmp_path, monkeypatch):
    root = tmp_path / "qa"
    root.mkdir()
    (root / "market_hub_process.json").write_text(
        json.dumps({"pid": 1234, "created": 100.0,
                    "leader_owner": "1234:lease", "bybit_ws_books": False}),
        encoding="utf-8",
    )
    monkeypatch.setattr(hub_supervisor, "_matches", lambda record, cache: True)
    with pytest.raises(hub_supervisor.HubUnavailable, match="no Bybit orderbook WebSocket"):
        hub_supervisor.ensure_hub(root, bybit_ws=True)


def test_ws_capability_cannot_be_supplied_as_truthy_string(tmp_path):
    with pytest.raises(ValueError, match="boolean"):
        hub_supervisor.ensure_hub(tmp_path / "unused", bybit_ws="1")
