"""Operator thread budgets must reach the production service CLI unchanged."""
import subprocess
import sys
from pathlib import Path
import pytest
from bcc.market_hub import HubRegistry


@pytest.mark.parametrize('workers', [13, 29])
def test_service_accepts_resource_budget_and_releases_its_lease(tmp_path, workers):
    path = tmp_path / 'public.sqlite'
    result = subprocess.run([sys.executable, '-m', 'tools.market_hub_service', '--cache', str(path),
                             '--concurrency', str(workers), '--poll', '.02', '--idle-exit', '.06'],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'leader lease acquired' in result.stdout
    assert HubRegistry(path).live_leader() is None


@pytest.mark.parametrize('workers', [0, -1])
def test_service_rejects_invalid_budget_without_creating_cache(tmp_path, workers):
    path = tmp_path / 'public.sqlite'
    result = subprocess.run([sys.executable, '-m', 'tools.market_hub_service', '--cache', str(path),
                             '--concurrency', str(workers)], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 2
    assert not path.exists()
