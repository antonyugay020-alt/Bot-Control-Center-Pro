"""Native Qt acceptance for exactly two VELES authoring levels."""

import os
if os.name != "nt" and not os.environ.get("DISPLAY"):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QLineEdit, QCheckBox, QMessageBox

from bcc.config import defaults, validate
from bcc.section_hierarchy import grouped_ranges, rsi_bounds
from bcc.widgets import VelesSectionsEditor


@pytest.fixture(scope="module")
def app():
    app = QApplication.instance() or QApplication([])
    yield app


def test_two_level_tree_and_legacy_round_trip(app):
    cfg = defaults("VELES")
    cfg["name"] = "Qt legacy view"
    editor = VelesSectionsEditor(cfg["sections"], cfg["settings"])
    editor.show()
    app.processEvents()
    assert editor.table.topLevelItemCount() == 1
    group = editor.table.topLevelItem(0)
    assert group.childCount() == 2
    assert group.child(0).text(0).startswith("Диапазон · RSI 30")
    assert group.child(1).text(0).startswith("Диапазон · RSI 40")
    assert editor.value_data() == cfg["sections"]
    editor.close()


def test_add_range_under_selected_section_with_working_config(app, monkeypatch):
    import bcc.widgets as widgets
    cfg = defaults("VELES")
    cfg["name"] = "Qt child insert"
    editor = VelesSectionsEditor(cfg["sections"], cfg["settings"])
    editor.table.setCurrentItem(editor.table.topLevelItem(0))
    monkeypatch.setattr(widgets, "confirm_dialog", lambda *a, **kw: True)
    before_ids = [s["id"] for s in cfg["sections"]]
    editor.add_range()
    result = editor.value_data()
    assert len(result) == 3
    assert [s["id"] for s in result[:2]] == before_ids
    assert rsi_bounds(result[2]) == (50, 60, 2, 1)
    assert len(grouped_ranges(result)) == 1
    assert editor.table.topLevelItemCount() == 1
    assert editor.table.topLevelItem(0).childCount() == 3
    cfg["sections"] = result
    validate(cfg)
    editor.close()


def test_renaming_section_does_not_touch_range_ids_or_signals(app, monkeypatch):
    import bcc.widgets as widgets
    cfg = defaults("VELES")
    cfg["name"] = "Qt group rename"
    editor = VelesSectionsEditor(cfg["sections"], cfg["settings"])
    editor.table.setCurrentItem(editor.table.topLevelItem(0))
    ids = [s["id"] for s in cfg["sections"]]
    signals = [(s["entry"], s["exit"]) for s in cfg["sections"]]

    def save_name(widget, *_args, **_kwargs):
        widget.findChildren(QLineEdit)[0].setText("BTC LONG RSI")
        return True

    monkeypatch.setattr(widgets, "confirm_dialog", save_name)
    editor.edit()
    cfg["sections"] = editor.value_data()
    validate(cfg)
    assert [s["id"] for s in cfg["sections"]] == ids
    assert [(s["entry"], s["exit"]) for s in cfg["sections"]] == signals
    assert grouped_ranges(cfg["sections"])[0]["name"] == "BTC LONG RSI"
    editor.close()


def test_rename_mixed_enabled_section_preserves_each_range(app, monkeypatch):
    """Editing a parent label must not disable individually enabled ranges."""
    import bcc.widgets as widgets
    from bcc.section_hierarchy import adopt_group
    cfg = defaults("VELES")
    cfg["name"] = "Qt mixed-enabled rename"
    group = grouped_ranges(cfg["sections"])[0]
    adopt_group(cfg["sections"], group, "RSI mixed")
    cfg["sections"][1]["enabled"] = False
    editor = VelesSectionsEditor(cfg["sections"], cfg["settings"])
    editor.table.setCurrentItem(editor.table.topLevelItem(0))

    def rename_only(widget, *_a, **_kw):
        widget.findChildren(QLineEdit)[0].setText("Renamed group")
        state = next(w for w in widget.findChildren(QCheckBox)
                     if w.text() == "Включить все диапазоны секции")
        assert state.checkState().name == "PartiallyChecked"
        return True

    monkeypatch.setattr(widgets, "confirm_dialog", rename_only)
    editor.edit()
    result = editor.value_data()
    assert [s["enabled"] for s in result] == [True, False]
    assert grouped_ranges(result)[0]["name"] == "Renamed group"
    cfg["sections"] = result
    validate(cfg)
    editor.close()


def test_explicit_enable_all_changes_mixed_section(app, monkeypatch):
    import bcc.widgets as widgets
    from bcc.section_hierarchy import adopt_group
    from PySide6.QtCore import Qt
    cfg = defaults("VELES")
    adopt_group(cfg["sections"], grouped_ranges(cfg["sections"])[0])
    cfg["sections"][1]["enabled"] = False
    editor = VelesSectionsEditor(cfg["sections"], cfg["settings"])
    editor.table.setCurrentItem(editor.table.topLevelItem(0))

    def enable_all(widget, *_a, **_kw):
        state = next(w for w in widget.findChildren(QCheckBox)
                     if w.text() == "Включить все диапазоны секции")
        state.setCheckState(Qt.CheckState.Checked)
        return True

    monkeypatch.setattr(widgets, "confirm_dialog", enable_all)
    editor.edit()
    assert all(s["enabled"] for s in editor.value_data())
    editor.close()


def test_last_range_cannot_be_deleted_and_other_removals_are_confirmed(app, monkeypatch):
    import bcc.widgets as widgets
    cfg = defaults("VELES")
    cfg["sections"] = cfg["sections"][:1]
    editor = VelesSectionsEditor(cfg["sections"], cfg["settings"])
    class FakeMessageBox:
        StandardButton = QMessageBox.StandardButton
        question = staticmethod(lambda *_a, **_kw: QMessageBox.StandardButton.Yes)
        warning = staticmethod(lambda *_a, **_kw: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(widgets, "QMessageBox", FakeMessageBox)
    editor.table.setCurrentItem(editor.table.topLevelItem(0).child(0))
    editor.remove()
    assert len(editor.value_data()) == 1
    editor.table.setCurrentItem(editor.table.topLevelItem(0))
    editor.remove()
    assert len(editor.value_data()) == 1
    editor.close()
