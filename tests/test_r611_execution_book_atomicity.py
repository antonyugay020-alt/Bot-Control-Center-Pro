"""Malformed depth must never produce a partial PAPER fill or mutate liquidity."""

import math
from copy import deepcopy

import pytest

from bcc.execution import ExecutionRejected, PaperExecutor
from test_core import book, config


@pytest.mark.parametrize(
    "bad_row",
    [
        (100.02, 1),            # Immutable: failure during depth mutation.
        ["100.02", 1],        # String-like prices are not executable prices.
        [100.02, "1"],        # String-like sizes are not executable sizes.
        [100.02, math.nan],
        [100.02, math.inf],
        [100.02, -1],
        [True, 1],
        [100.02, False],
        [100.02],
        [100.02, 1, "extra"],
        None,
    ],
)
def test_malformed_second_ask_rejected_without_consuming_first(bad_row):
    snapshot = book(size=1)
    snapshot["asks"] = [[100.01, 1], bad_row]
    before = deepcopy(snapshot)
    settings = config()["settings"]
    settings["slippage_pct"] = 0
    with pytest.raises(ExecutionRejected, match="повреждённый"):
        PaperExecutor(snapshot).estimate("LONG", "ENTRY", settings, quantity=1.5)
    assert snapshot == before


@pytest.mark.parametrize("side,leg,field", [
    ("LONG", "ENTRY", "asks"),
    ("SHORT", "ENTRY", "bids"),
    ("LONG", "EXIT", "bids"),
    ("SHORT", "EXIT", "asks"),
])
def test_unsorted_side_rejected_without_depth_mutation(side, leg, field):
    snapshot = book(size=2)
    snapshot[field].reverse()
    before = deepcopy(snapshot)
    with pytest.raises(ExecutionRejected, match="порядок"):
        PaperExecutor(snapshot).estimate(side, leg, config()["settings"], quantity=1)
    assert snapshot == before


def test_valid_multilevel_paper_fill_remains_atomic_and_executable():
    snapshot = book(size=1)
    settings = config()["settings"]
    settings["slippage_pct"] = 0
    fill = PaperExecutor(snapshot).estimate("LONG", "ENTRY", settings, quantity=1.5)
    assert fill.quantity == pytest.approx(1.5)
    assert snapshot["asks"][0][1] == pytest.approx(0)
    assert snapshot["asks"][1][1] == pytest.approx(0.5)
