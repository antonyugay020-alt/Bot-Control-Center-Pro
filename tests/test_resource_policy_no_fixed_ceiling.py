"""Adjustable producer threads and risk limits are distinct from bot count."""
from types import SimpleNamespace
import pytest
from bcc.market_hub import MarketDataHub
from bcc.shared_risk import validate_policy

@pytest.mark.parametrize("workers", [1, 13, 29])
def test_public_hub_accepts_operator_selected_concurrency(tmp_path, workers):
    source = SimpleNamespace(shared=object(), close=lambda: None)
    hub = MarketDataHub(tmp_path / f'hub-{workers}.sqlite', source, max_workers=workers)
    try:
        assert hub.max_workers == workers
    finally:
        hub.close()

def test_optional_position_policy_does_not_impose_application_ceiling():
    for maximum in (0, 10**6 + 7, 10**12):
        policy = {"max_positions": maximum, "max_committed_usdt": 0.0}
        assert validate_policy(policy) == policy
    for invalid in (-1, True, 5.5, "10"):
        with pytest.raises(ValueError):
            validate_policy({"max_positions": invalid, "max_committed_usdt": 0.0})
