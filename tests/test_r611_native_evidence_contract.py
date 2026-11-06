"""Windows acceptance must count executed native Qt tests, not just JUnit totals."""
import xml.etree.ElementTree as ET

from test_r65_windows_evidence_gate import fixture, failures

QT_MODULES = (
    "test_qt", "test_poly_indicator_qt", "test_veles_hierarchy_qt",
    "test_wizard_visual_qt", "test_settings_tree_contrast",
)


def _edit_xml(path, fn):
    tree = ET.parse(path)
    fn(tree.getroot().find("testsuite") or tree.getroot())
    tree.write(path, encoding="utf-8")


def test_windows_gate_refuses_550_headless_tests_without_qt(tmp_path):
    fixture(tmp_path)
    xml = tmp_path / "tests.xml"
    def remove_qt(suite):
        for case in suite.findall("testcase"):
            if any((case.get("classname") or "").endswith(x) for x in QT_MODULES):
                case.set("classname", "tests.test_core")
    _edit_xml(xml, remove_qt)
    assert any("Qt" in problem for problem in failures(tmp_path))


def test_windows_gate_refuses_skipped_qt_test(tmp_path):
    fixture(tmp_path)
    xml = tmp_path / "tests.xml"
    def skip_qt(suite):
        case = next(c for c in suite.findall("testcase")
                    if (c.get("classname") or "").endswith("test_qt"))
        ET.SubElement(case, "skipped", message="skipped Qt test")
        suite.set("skipped", "1")
    _edit_xml(xml, skip_qt)
    assert any("Qt" in problem or "skipped" in problem.lower()
               for problem in failures(tmp_path))


def test_windows_gate_accepts_known_posix_only_skip(tmp_path):
    fixture(tmp_path)
    xml = tmp_path / "tests.xml"
    def skip_posix(suite):
        case = suite.findall("testcase")[0]
        case.set("classname", "tests.test_r611_qa_process_integrity")
        case.set("name", "test_successful_exit_with_live_child_cannot_pass")
        ET.SubElement(case, "skipped", message="POSIX process-group orphan check")
        suite.set("skipped", "1")
    _edit_xml(xml, skip_posix)
    # The fixture models a new *collected* test identity on Windows; both
    # collection inventory and JUnit must describe the same POSIX-only skip.
    import json
    inventory = tmp_path / "collected_tests.json"
    recorded = json.loads(inventory.read_text(encoding="utf-8"))
    recorded["cases"][0] = ["tests.test_r611_qa_process_integrity",
                             "test_successful_exit_with_live_child_cannot_pass"]
    inventory.write_text(json.dumps(recorded), encoding="utf-8")
    assert failures(tmp_path) == []


def test_windows_gate_rejects_unexpected_skip_even_if_totals_match(tmp_path):
    fixture(tmp_path)
    xml = tmp_path / "tests.xml"
    def unexpected(suite):
        ET.SubElement(suite.findall("testcase")[1], "skipped", message="test is unavailable")
        suite.set("skipped", "1")
    _edit_xml(xml, unexpected)
    assert any("skipped" in x.lower() or "пропущ" in x.lower()
               for x in failures(tmp_path))


def test_windows_gate_requires_actual_closed_paper_cycles(tmp_path):
    fixture(tmp_path)
    xml = tmp_path / "tests.xml"
    def remove_cycle(suite):
        case = next(c for c in suite.findall("testcase")
                    if c.get("name") == "test_polymarket_full_cycle_pause_reopen_official_settle_once")
        suite.remove(case)
        suite.set("tests", str(len(suite.findall("testcase"))))
    _edit_xml(xml, remove_cycle)
    # Even with syntactically correct JUnit, a removed actual trade case
    # must never be accepted as a completed PAPER cycle.
    assert any("PAPER" in problem for problem in failures(tmp_path))

def test_windows_gate_rejects_skipped_paper_recovery(tmp_path):
    fixture(tmp_path)
    xml = tmp_path / "tests.xml"
    def skip_cycle(suite):
        case = next(c for c in suite.findall("testcase")
                    if c.get("name") == "test_veles_full_close_open_close_on_one_sqlite_ledger")
        ET.SubElement(case, "skipped", message="paper ledger not checked")
        suite.set("skipped", "1")
    _edit_xml(xml, skip_cycle)
    assert any("PAPER" in problem for problem in failures(tmp_path))


def test_windows_gate_requires_separate_qt_process_reopen_with_worker_running(tmp_path):
    fixture(tmp_path)
    name = 'test_separate_qt_windows_reopen_running_account_inside_owned_acceptance_session'
    def replace_case(suite):
        case = next(c for c in suite.findall('testcase') if c.get('name') == name)
        case.set('classname', 'tests.test_unrelated')
    _edit_xml(tmp_path / 'tests.xml', replace_case)
    import json
    inventory = tmp_path / 'collected_tests.json'
    value = json.loads(inventory.read_text(encoding="utf-8"))
    for case in value['cases']:
        if case[1] == name:
            case[0] = 'tests.test_unrelated'
    inventory.write_text(json.dumps(value), encoding="utf-8")
    assert any(name in error for error in failures(tmp_path))
