"""Negative proof that minimum test counts cannot certify Windows QA."""
import json
import xml.etree.ElementTree as ET
import pytest

from tools.collect_test_inventory import case_identity
from tools.verify_windows import verify_complete_test_inventory


def evidence(tmp_path, collected, executed):
    inventory = tmp_path / "collected_tests.json"
    junit = tmp_path / "tests.xml"
    inventory.write_text(json.dumps({"schema": 1, "cases": collected}), encoding="utf-8")
    root = ET.Element("testsuite", tests=str(len(executed)), failures="0", errors="0", skipped="0")
    for classname, name in executed:
        ET.SubElement(root, "testcase", classname=classname, name=name)
    ET.ElementTree(root).write(junit, encoding="utf-8")
    return junit, inventory


def test_normal_and_parameterized_test_ids():
    assert case_identity("tests/test_demo.py::test_one") == ["tests.test_demo", "test_one"]
    assert case_identity("tests/test_demo.py::TestCase::test_two[USD-BTC]") == ["tests.test_demo.TestCase", "test_two[USD-BTC]"]


def test_exact_expected_set_passes(tmp_path):
    rows = [["tests.test_a", "test_a"], ["tests.test_b.TestC", "test_b[2]"]]
    assert verify_complete_test_inventory(*evidence(tmp_path, rows, rows)) == 2


@pytest.mark.parametrize("corruption", ["omitted", "unexpected", "duplicate", "misidentified", "duplicate_inventory"])
def test_forged_or_incomplete_execution_is_rejected(tmp_path, corruption):
    expected = [["tests.test_a", "test_a"], ["tests.test_b", "test_b"]]
    actual = [x[:] for x in expected]
    if corruption == "omitted": actual.pop()
    if corruption == "unexpected": actual.append(["tests.unknown", "unexpected"])
    if corruption == "duplicate": actual.append(actual[0])
    if corruption == "misidentified": actual[1][1] = "test_c"
    if corruption == "duplicate_inventory": expected.append(expected[0])
    with pytest.raises(ValueError):
        verify_complete_test_inventory(*evidence(tmp_path, expected, actual))


def test_inventory_missing_fails_closed(tmp_path):
    rows = [["tests.test_a", "test_a"]]
    junit, inventory = evidence(tmp_path, rows, rows)
    inventory.unlink()
    with pytest.raises(OSError):
        verify_complete_test_inventory(junit, inventory)
