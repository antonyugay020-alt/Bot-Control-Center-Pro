"""Fail closed when a PAPER valuation receives unordered price levels."""
import pytest

from bcc.market import DataUnavailable, executable_quote
from bcc.runtime import process_tick
from bcc.scheduler import MarketTick
from bcc.storage import Store
from test_core import book, config


@pytest.mark.parametrize("side,prices", [
    ("bids", (99.0, 99.5)),
    ("bids", (99.0, 99.0)),
    ("asks", (101.0, 100.5)),
    ("asks", (101.0, 101.0)),
])
def test_unordered_quote_rejected_even_if_top_spread_looks_safe(side, prices):
    snapshot = book(price=100)
    snapshot[side] = [[price, 5.0] for price in prices]
    with pytest.raises(DataUnavailable, match="порядок"):
        executable_quote(snapshot)


def test_quote_ignores_undeployable_phantom_levels_but_validates_executable_order():
    snapshot = book(price=100)
    snapshot["bids"] = [[1, 0], [99.99, 5], [100.5, 0], [99.98, 5]]
    assert executable_quote(snapshot) == (99.99, 100.01)
    snapshot["bids"][-1][0] = 100.4
    with pytest.raises(DataUnavailable, match="порядок"):
        executable_quote(snapshot)


def test_invalid_quote_never_enters_accounting_transaction(tmp_path):
    store = Store(tmp_path, 500)
    before = store.read(include_charts=False)
    snapshot = book(price=100)
    snapshot["asks"] = [[101, 4], [100.5, 4]]

    class MustNotTrade:
        def process(self, *args):
            raise AssertionError("An invalid book reached the strategy")

    with pytest.raises(DataUnavailable, match="порядок"):
        process_tick(config(), store, MustNotTrade(),
                     MarketTick("BTCUSDT", [], snapshot, [], {}))
    after = store.read(include_charts=False)
    assert after == before
    store.audit()


def test_valid_ordered_quote_skips_zero_depth_but_preserves_best_executable():
    snapshot = book(price=100)
    snapshot["bids"] = [[99.99, 0], [99.98, 2]]
    snapshot["asks"] = [[100.01, 0], [100.02, 2]]
    assert executable_quote(snapshot) == (99.98, 100.02)
