"""Native gate cannot accept fake JUnit totals without executed test cases."""
import pytest
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from test_r65_windows_evidence_gate import failures, fixture, verifier


def test_forged_suite_without_cases_cannot_certify_windows(tmp_path):
    fixture(tmp_path)
    xml = tmp_path / "tests.xml"
    xml.write_text('<testsuite tests="550" failures="0" errors="0" skipped="0"/>', encoding="utf-8")
    assert any("tests.xml" in problem for problem in failures(tmp_path))


def test_suite_count_mismatch_rejected(tmp_path):
    fixture(tmp_path)
    xml = tmp_path / "tests.xml"
    tree = ET.parse(xml)
    tree.getroot().set('tests', str(int(tree.getroot().get('tests')) + 1))
    tree.write(xml, encoding='utf-8')
    with pytest.raises(ValueError, match="testcase"):
        verifier().read_test_totals(xml)


def test_reported_success_cannot_hide_failed_testcase(tmp_path):
    fixture(tmp_path)
    xml = tmp_path / "tests.xml"
    xml.write_text(xml.read_text(encoding="utf-8").replace(
        '<testcase classname="bcc" name="test_1" />',
        '<testcase classname="bcc" name="test_1"><failure /></testcase>', 1), encoding="utf-8")
    with pytest.raises(ValueError, match="testcase"):
        verifier().read_test_totals(xml)


def test_full_case_count_is_preserved(tmp_path):
    fixture(tmp_path)
    expected = len(json.loads((tmp_path / 'collected_tests.json').read_text(encoding="utf-8"))['cases'])
    assert verifier().read_test_totals(tmp_path / "tests.xml") == {
        "tests": expected, "failures": 0, "errors": 0, "skipped": 0}
    assert failures(tmp_path) == []
