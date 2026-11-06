"""Regression: two levels only, legacy configs / IDs are not rewritten."""

from copy import deepcopy

import pytest

from bcc.config import defaults, validate, group, condition
from bcc.section_hierarchy import (PARENT_ID, PARENT_LABEL, adopt_group,
                                   append_rsi_range, grouped_ranges, rsi_bounds, replace_range)


def test_default_legacy_ranges_group_for_display_without_mutating_config():
    cfg = defaults("VELES")
    cfg["name"] = "Section hierarchy test"
    original = deepcopy(cfg)
    groups = grouped_ranges(cfg["sections"])
    assert len(groups) == 1
    assert len(groups[0]["sections"]) == 2
    assert [rsi_bounds(s) for s in groups[0]["sections"]] == [
        (30, 40, 2, 1), (40, 50, 2, 1)
    ]
    assert cfg == original
    validate(cfg)


def test_group_can_gain_range_without_changing_existing_execution_ids():
    cfg = defaults("VELES")
    cfg["name"] = "Section hierarchy test"
    ids_before = [s["id"] for s in cfg["sections"]]
    g = grouped_ranges(cfg["sections"])[0]
    parent = adopt_group(cfg["sections"], g, "BTC LONG RSI")
    added = append_rsi_range(cfg["sections"], parent, "BTC LONG RSI", 50, 60)
    assert [s["id"] for s in cfg["sections"][:2]] == ids_before
    assert len({s["id"] for s in cfg["sections"]}) == 3
    assert len(grouped_ranges(cfg["sections"])) == 1
    assert all(s[PARENT_ID] == parent and s[PARENT_LABEL] == "BTC LONG RSI"
               for s in cfg["sections"])
    assert rsi_bounds(added) == (50, 60, 2, 1)
    validate(cfg)


def test_grouped_compound_filters_are_preserved_when_range_added():
    cfg = defaults("VELES")
    cfg["name"] = "Section hierarchy test"
    cfg["sections"] = cfg["sections"][:1]
    cfg["sections"][0]["entry"]["conditions"].append(
        condition("REL_VOLUME", 3, ">", 1))
    g = grouped_ranges(cfg["sections"])[0]
    gid = adopt_group(cfg["sections"], g)
    new = append_rsi_range(cfg["sections"], gid, g["name"], 40, 50)
    assert len(new["entry"]["conditions"]) == 2
    assert new["entry"]["conditions"][1]["indicator"] == "REL_VOLUME"
    assert new["entry"]["conditions"][0]["value"] == 40
    validate(cfg)


def test_invalid_group_metadata_or_duplicate_ranges_blocked():
    cfg = defaults("VELES")
    cfg["name"] = "Section hierarchy test"
    g = grouped_ranges(cfg["sections"])[0]
    gid = adopt_group(cfg["sections"], g)
    with pytest.raises(ValueError, match="существует"):
        append_rsi_range(cfg["sections"], gid, g["name"], 30, 40)
    cfg["sections"][1][PARENT_LABEL] = "wrong"
    with pytest.raises(ValueError, match="одной секции"):
        validate(cfg)


def test_short_parent_direction_and_range():
    from bcc.config import range_section
    cfg = defaults("VELES")
    cfg["name"] = "Section hierarchy test"
    cfg["sections"] = [range_section(70, 60, "SHORT")]
    g = grouped_ranges(cfg["sections"])[0]
    gid = adopt_group(cfg["sections"], g, "SHORT")
    added = append_rsi_range(cfg["sections"], gid, "SHORT", 60, 50)
    assert rsi_bounds(added) == (60, 50, 2, 1)
    with pytest.raises(ValueError, match="Направление"):
        append_rsi_range(cfg["sections"], gid, "SHORT", 50, 60)
    validate(cfg)


def test_repository_round_trip_preserves_hierarchy_and_existing_range_ids(tmp_path):
    from bcc.storage import Repository
    cfg = defaults("VELES")
    cfg["name"] = "Section Range Roundtrip"
    initial_ids = [s["id"] for s in cfg["sections"]]
    group = grouped_ranges(cfg["sections"])[0]
    gid = adopt_group(cfg["sections"], group, "Main RSI")
    append_rsi_range(cfg["sections"], gid, "Main RSI", 50, 60)
    repo = Repository(tmp_path)
    folder = repo.create(cfg)
    restored, digest = repo.load(folder)
    assert [s["id"] for s in restored["sections"][:2]] == initial_ids
    assert [(s["range_parent_id"], s["range_parent_label"]) for s in restored["sections"]] == [
        (gid, "Main RSI"), (gid, "Main RSI"), (gid, "Main RSI")]
    assert [rsi_bounds(s) for s in restored["sections"]] == [
        (30, 40, 2, 1), (40, 50, 2, 1), (50, 60, 2, 1)]
    validate(restored)
    assert repo.load(folder)[1] == digest


@pytest.mark.parametrize("bounds", [
    (-1, 40), (30, 101), (float("nan"), 40), (30, float("inf")),
    (True, 40), (30, float("-inf")),
])
def test_programmatic_range_rejects_invalid_rsi_bounds(bounds):
    cfg = defaults("VELES")
    parent = adopt_group(cfg["sections"], grouped_ranges(cfg["sections"])[0])
    original = deepcopy(cfg["sections"])
    with pytest.raises(ValueError, match="Уровни RSI"):
        append_rsi_range(cfg["sections"], parent, "RSI", *bounds)
    assert cfg["sections"] == original


@pytest.mark.parametrize("entry_tf,exit_tf", [(3, 1), (2, 60), (2.0, 1), (2, True)])
def test_programmatic_range_rejects_invalid_timeframes(entry_tf, exit_tf):
    cfg = defaults("VELES")
    parent = adopt_group(cfg["sections"], grouped_ranges(cfg["sections"])[0])
    original = deepcopy(cfg["sections"])
    with pytest.raises(ValueError, match="таймфрейм"):
        append_rsi_range(cfg["sections"], parent, "RSI", 50, 60, entry_tf, exit_tf)
    assert cfg["sections"] == original


def test_independent_entry_exit_triggers_still_display_one_section():
    cfg = defaults("VELES")
    cfg["name"] = "Independent triggers"
    cfg["sections"][0]["exit"]["conditions"][0]["trigger"] = "intrabar"
    cfg["sections"][1]["exit"]["conditions"][0]["trigger"] = "intrabar"
    original = deepcopy(cfg)
    grouped = grouped_ranges(cfg["sections"])
    assert len(grouped) == 1
    assert len(grouped[0]["sections"]) == 2
    assert rsi_bounds(cfg["sections"][0]) == (30, 40, 2, 1)
    assert cfg == original
    validate(cfg)


def test_legacy_ranges_with_different_trigger_pairs_not_inferred_as_one_section():
    cfg = defaults("VELES")
    cfg["sections"][1]["exit"]["conditions"][0]["trigger"] = "intrabar"
    grouped = grouped_ranges(cfg["sections"])
    assert len(grouped) == 2
    assert [len(group["sections"]) for group in grouped] == [1, 1]


def test_edit_legacy_child_disabling_it_keeps_two_ranges_in_same_parent():
    cfg = defaults("VELES")
    cfg["name"] = "Edit legacy range"
    old_ids = [s["id"] for s in cfg["sections"]]
    edited = deepcopy(cfg["sections"][1])
    edited["enabled"] = False
    updated = replace_range(cfg["sections"], old_ids[1], edited)
    assert [s["id"] for s in updated] == old_ids
    assert [s["enabled"] for s in updated] == [True, False]
    assert len(grouped_ranges(updated)) == 1
    assert len(grouped_ranges(updated)[0]["sections"]) == 2
    cfg["sections"] = updated
    validate(cfg)


def test_edit_individual_risk_does_not_split_section_or_alter_other_range():
    cfg = defaults("VELES")
    cfg["name"] = "Edit one range"
    original = deepcopy(cfg["sections"])
    edited = deepcopy(cfg["sections"][0])
    edited["overrides"] = {"disabled": True}
    updated = replace_range(cfg["sections"], edited["id"], edited)
    assert updated[1]["entry"] == original[1]["entry"]
    assert updated[1]["overrides"] == original[1]["overrides"]
    assert updated[1]["id"] == original[1]["id"]
    assert len(grouped_ranges(updated)) == 1
    cfg["sections"] = updated
    validate(cfg)


def test_unchanged_legacy_child_remains_unchanged_and_id_cannot_change():
    cfg = defaults("VELES")
    original = deepcopy(cfg["sections"])
    assert replace_range(original, original[0]["id"], deepcopy(original[0])) == original
    damaged = deepcopy(original[0]); damaged["id"] = "replacement-id"
    with pytest.raises(ValueError, match="ID диапазона"):
        replace_range(original, original[0]["id"], damaged)
    assert original == cfg["sections"]


def test_repository_edit_range_keeps_open_paper_section_and_ledger(tmp_path):
    """Hierarchy-only migration must not orphan a pre-existing PAPER position."""
    from bcc.storage import Repository, Store
    cfg = defaults("VELES")
    cfg["name"] = "Keep open range"
    cfg["symbols"] = ["BTCUSDT"]
    original_ids = [s["id"] for s in cfg["sections"]]
    repo = Repository(tmp_path)
    folder = repo.create(cfg)
    store = Store(folder, cfg["capital"])
    pos_id = f"BTCUSDT:{original_ids[0]}"
    with store.transaction() as (state, events):
        state["positions"][pos_id] = dict(
            kind="VELES", symbol="BTCUSDT", section=original_ids[0],
            side="LONG", qty=1, average=100, margin=10,
            order_margin=0,
        )
    before = store.read()
    edited, digest = repo.load(folder)
    child = deepcopy(edited["sections"][1])
    child["enabled"] = False
    edited["sections"] = replace_range(edited["sections"], child["id"], child)
    repo.save(folder, edited, digest)
    after_config, _ = repo.load(folder)
    assert [s["id"] for s in after_config["sections"]] == original_ids
    assert len(grouped_ranges(after_config["sections"])) == 1
    assert Store(folder).read() == before


def test_new_short_range_normalizes_inherited_opposite_crossings_without_touching_parent():
    from bcc.config import range_section
    cfg = defaults("VELES")
    cfg["name"] = "Short mismatch import"
    original = range_section(70, 60, "SHORT")
    original["entry"]["conditions"][0]["comparison"] = "cross_up"
    original["exit"]["conditions"][0]["comparison"] = "cross_up"
    original["entry"]["conditions"].append(condition("REL_VOLUME", 3, ">", 1))
    cfg["sections"] = [original]
    gid = adopt_group(cfg["sections"], grouped_ranges(cfg["sections"])[0])
    before = deepcopy(cfg["sections"][0])
    created = append_rsi_range(cfg["sections"], gid, "Short mismatch", 60, 50)
    assert cfg["sections"][0] == before
    assert created["entry"]["conditions"][0]["comparison"] == "cross_down"
    assert created["exit"]["conditions"][0]["comparison"] == "cross_down"
    assert created["entry"]["conditions"][1] == before["entry"]["conditions"][1]
    assert rsi_bounds(created) is None  # compound volume filter retained


@pytest.mark.parametrize("side,start,end,inherited", [
    ("LONG", 40, 50, "cross_down"),
    ("SHORT", 60, 50, "cross_up"),
])
def test_new_range_fixes_both_rsi_crossings_for_direction(side, start, end, inherited):
    from bcc.config import range_section
    cfg = defaults("VELES")
    base = range_section(30, 40, side) if side == "LONG" else range_section(70, 60, side)
    base["entry"]["conditions"][0]["comparison"] = inherited
    cfg["sections"] = [base]
    gid = adopt_group(cfg["sections"], grouped_ranges(cfg["sections"])[0])
    old = deepcopy(cfg["sections"][0])
    new = append_rsi_range(cfg["sections"], gid, "RSI", start, end)
    assert cfg["sections"][0] == old
    expected = "cross_up" if side == "LONG" else "cross_down"
    assert [new[key]["conditions"][0]["comparison"] for key in ("entry", "exit")] == [expected, expected]


def test_new_range_rejects_non_crossing_source_without_mutation():
    cfg = defaults("VELES")
    gid = adopt_group(cfg["sections"], grouped_ranges(cfg["sections"])[0])
    cfg["sections"][0]["entry"]["conditions"][0]["comparison"] = ">"
    before = deepcopy(cfg["sections"])
    with pytest.raises(ValueError, match="пересечение"):
        append_rsi_range(cfg["sections"], gid, "RSI", 50, 60)
    assert cfg["sections"] == before
