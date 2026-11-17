"""Prevent an unused public transport from contaminating offline OS regression."""
import pytest

from bcc.hub_supervisor import ensure_hub
from conftest import cleanup_owned_test_hubs


def test_live_producer_is_rejected_before_spawn_and_real_offline_hub_still_recovers(tmp_path):
    with pytest.raises(AssertionError, match='uncontrolled live WS Hub'):
        ensure_hub(tmp_path, bybit_ws=True)
    assert not (tmp_path/'market_hub_process.json').exists()
    # The guard does not replace the actual OS lifecycle or grant fake
    # readiness. The ordinary replay-capable Hub must still start and retire.
    first = ensure_hub(tmp_path)
    assert ensure_hub(tmp_path) == first
    cleanup_owned_test_hubs(tmp_path)
    second = ensure_hub(tmp_path)
    assert second != first
    cleanup_owned_test_hubs(tmp_path)
