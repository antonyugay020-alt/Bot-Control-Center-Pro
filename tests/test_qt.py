"""Actual Qt widgets and event dispatch; no fake widget implementations."""

import os

if os.name != "nt" and not os.environ.get("DISPLAY"):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from bcc.config import defaults, validate
from bcc.ui import MainWindow, apply_style
from bcc.widgets import GroupEditor, SectionEditor
from bcc.wizard import BotWizard


@pytest.fixture(scope="module")
def app():
    app = QApplication.instance() or QApplication([])
    apply_style(app, {"scale": 100, "theme": "dark"})
    yield app
    # Native widgets must be destroyed while QApplication and their Python
    # virtual methods still exist, before unordered interpreter teardown.
    from PySide6.QtCore import QCoreApplication, QEvent

    app.closeAllWindows()
    for widget in app.topLevelWidgets():
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


@pytest.mark.parametrize(
    "kind",
    [
        "VELES",
        "POLYMARKET",
        "PUMP_DUMP",
        "GRID",
        "EMA",
        "MACD",
        "BOLLINGER",
        "EMA_RSI_VOLUME",
    ],
)
def test_create_edit_all_pages_without_losing_settings(app, kind, tmp_path):
    cfg = defaults(kind)
    cfg["name"] = "Qt " + kind
    dialog = BotWizard(cfg)
    dialog.show()
    app.processEvents()
    for page in range(6):
        dialog.steps.setCurrentRow(page)
        app.processEvents()
        validate(dialog.data())
        if page == 1:
            assert dialog.finance_widget.isVisible()
            assert dialog.settings.finance.controls["daily_loss_pct"][1].isVisible()
        if page == 3 and kind in (
            "VELES",
            "EMA",
            "MACD",
            "BOLLINGER",
            "EMA_RSI_VOLUME",
        ):
            assert dialog.settings.orders.isVisible()
            assert dialog.settings.orders.controls["count"][1].isVisible()
    assert dialog.data() == cfg
    dialog.name.selectAll()
    QTest.keyClicks(dialog.name, "Edited")
    dialog.commit()
    assert dialog.result_config["name"] == "Edited"
    from bcc.storage import Repository

    repo = Repository(tmp_path)
    folder = repo.create(dialog.result_config)
    saved, digest = repo.load(folder)
    again = BotWizard(saved)
    again.show()
    app.processEvents()
    assert again.data() == saved
    again.close()
    dialog.close()


def test_visible_veles_finance_and_grid_can_be_edited_with_keyboard_and_reopened(
    app, tmp_path
):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLineEdit

    from bcc.storage import Repository

    cfg = defaults()
    cfg["name"] = "Visible VELES settings"
    wizard = BotWizard(cfg)
    wizard.show()
    for page, form, key, value in [
        (1, wizard.settings.finance, "leverage", 7),
        (3, wizard.settings.orders, "count", 6),
    ]:
        wizard.steps.setCurrentRow(page)
        app.processEvents()
        control = form.controls[key][1]
        assert control.isVisible() and control.isEnabled()
        wizard.settings_pages[0 if page == 1 else 1].ensureWidgetVisible(control)
        app.processEvents()
        field = control.findChild(QLineEdit)
        QTest.mouseClick(field, Qt.MouseButton.LeftButton)
        field.selectAll()
        QTest.keyClicks(field, str(value))
        QTest.keyClick(field, Qt.Key.Key_Tab)
        assert control.value() == value
    wizard.commit()
    repo = Repository(tmp_path)
    folder = repo.create(wizard.result_config)
    saved, _ = repo.load(folder)
    assert saved["settings"]["leverage"] == 7
    assert saved["settings"]["orders"]["count"] == 6
    reopened = BotWizard(saved)
    reopened.show()
    reopened.steps.setCurrentRow(3)
    app.processEvents()
    assert reopened.settings.orders.controls["count"][1].isVisible()
    assert reopened.settings.orders.controls["count"][1].value() == 6
    reopened.close()
    wizard.close()


def wait_for_monitor(app, monitor, predicate, seconds=12):
    """Await the asynchronous full-fleet snapshot using the real Qt event loop.

    A synchronous row assertion after refresh() checks thread scheduling,
    not the global monitor contract. Never invent a snapshot or call its slot.
    """
    import time
    from PySide6.QtTest import QTest

    requested = monitor._requested_snapshot
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if monitor.completed_snapshot >= requested and predicate():
            return
        QTest.qWait(25)
    raise AssertionError(
        f"Monitor snapshot incomplete: loading={monitor._loading}, "
        f"rows={monitor.bots.rowCount()}, positions={monitor.positions.rowCount()}, "
        f"error={monitor.alert.text()!r}"
    )


def test_running_monitor_combines_real_workers_independently_of_main_filter(
    app, tmp_path
):
    from PySide6.QtCore import Qt
    from test_process import RUNNER, launch, setup_bot, wait_for

    from bcc.storage import Store, atomic_json

    win = MainWindow(tmp_path / "application")
    runner = tmp_path / "monitor_worker.py"
    runner.write_text(RUNNER, encoding="utf-8")
    veles, frame = setup_bot(win.repo, runner, "Same name")
    cfg = defaults("GRID")
    cfg.update(name="Same name", symbols=["BTCUSDT"])
    grid = win.repo.create(cfg)
    atomic_json(grid / "feed.json", frame)
    children = []
    try:
        for folder in (veles, grid):
            children.append(launch(win.life, folder, runner))
            wait_for(lambda f=folder: Store(f).read()["positions"])
        win.select_strategy("VELES")
        win.search.setText("BTC")
        win.show_running_bots()
        app.processEvents()
        monitor = win.running_monitor
        assert monitor.isVisible() and monitor.timer.isActive()
        wait_for_monitor(app, monitor, lambda: monitor.bots.rowCount() == 2
                         and monitor.positions.rowCount() == 2)
        assert {r["config"]["strategy"] for r in monitor.records} == {"VELES", "GRID"}
        monitor.bots.sortByColumn(2, Qt.SortOrder.DescendingOrder)
        for row in range(monitor.bots.rowCount()):
            if monitor.bots.item(row, 0).data(Qt.ItemDataRole.UserRole) == str(grid):
                monitor.bots.selectRow(row)
                break
        monitor.open_selected()
        assert win.current == grid
        monitor.tabs.setCurrentWidget(monitor.history)
        monitor.refresh()
        wait_for_monitor(app, monitor, lambda: monitor.history.rowCount() >= 2)
        monitor.close()
        assert not monitor.timer.isActive()
        assert all(c.poll() is None for c in children)
        assert not (veles / "stop.request").exists()
        win.show_running_bots()
        assert win.running_monitor is monitor
        win.life.stop(grid)
        children[1].wait(timeout=7)
        monitor.refresh()
        wait_for_monitor(app, monitor, lambda: monitor.bots.rowCount() == 2
                         and any(r["status"] == "Остановлен" for r in monitor.records))
        # Stopped bot still owns an open position.
        assert (
            "приостановлено" in monitor.positions.item(1, 9).text()
            or "приостановлено" in monitor.positions.item(0, 9).text()
        )
        monitor.include_stopped.setChecked(True)
        wait_for_monitor(app, monitor, lambda: monitor.bots.rowCount() == 2)
        assert monitor.positions.rowCount() == 2
    finally:
        for folder, child in zip((veles, grid), children):
            if child.poll() is None:
                win.life.stop(folder)
                child.wait(timeout=7)
        if win.running_monitor:
            win.running_monitor.close()
        win.close()


def test_independent_timeframes_directions_and_cancel(app):
    cfg = defaults()
    sec = cfg["sections"][0]
    edit = SectionEditor(sec, cfg["settings"])
    entry = edit.entry.rows[0]
    exit_ = edit.exit.rows[0]
    entry.tf.setCurrentIndex(entry.tf.findData(10))
    exit_.tf.setCurrentIndex(exit_.tf.findData(5))
    entry.trigger.setCurrentIndex(entry.trigger.findData("intrabar"))
    exit_.trigger.setCurrentIndex(exit_.trigger.findData("bar_close"))
    result = edit.value_data()
    assert result["entry"]["conditions"][0]["timeframe"] == 10
    assert result["exit"]["conditions"][0]["timeframe"] == 5
    assert result["entry"]["conditions"][0]["trigger"] == "intrabar"
    assert cfg["sections"][0] == sec  # editor mutates a private draft
    edit.close()


def test_new_and_unstarted_bot_does_not_display_another_bots_positions(app, tmp_path):
    from bcc.storage import Store

    win = MainWindow(tmp_path)
    cfg = defaults()
    cfg["name"] = "One"
    first = win.repo.create(cfg)
    cfg["name"] = "Two"
    second = win.repo.create(cfg)
    Store(first, 500)
    win.current = first
    win.refresh()
    win.current = second
    win.refresh_details()
    assert win.positions.rowCount() == 0
    assert win.history.rowCount() == 0
    assert win.monitor_state is None
    win.close()


def test_nested_groups_can_roundtrip(app):
    from bcc.config import condition, group

    spec = {
        "operator": "OR",
        "conditions": [],
        "groups": [group(condition()), group(condition("SMA", 100, ">"))],
    }
    editor = GroupEditor(spec)
    assert editor.value_data() == spec
    editor.close()


def test_sorting_refresh_and_strategy_tree_preserve_bot_identity(app, tmp_path):
    from PySide6.QtCore import Qt

    win = MainWindow(tmp_path)
    folders = {}
    for capital, kind in [(2, "VELES"), (100, "GRID"), (20, "VELES")]:
        cfg = defaults(kind)
        cfg.update(name=f"Bot {capital}", capital=capital)
        folders[capital] = win.repo.create(cfg)
    win.refresh()
    for row in range(win.bots_table.rowCount()):
        if win.bots_table.item(row, 0).data(Qt.ItemDataRole.UserRole) == str(
            folders[20]
        ):
            win.bots_table.selectRow(row)
    assert win.selected() == folders[20]
    win.bots_table.sortByColumn(4, Qt.SortOrder.DescendingOrder)
    win.refresh()
    assert [float(win.bots_table.item(i, 4).text()) for i in range(3)] == [100, 20, 2]
    assert win.selected() == folders[20]
    assert win.bots_table.item(win.bots_table.currentRow(), 0).data(
        Qt.ItemDataRole.UserRole
    ) == str(folders[20])
    for i in range(win.bot_tree.topLevelItemCount()):
        parent = win.bot_tree.topLevelItem(i)
        if parent.data(0, Qt.ItemDataRole.UserRole + 1) == "GRID":
            win.bot_tree.setCurrentItem(parent.child(0))
            break
    assert win.selected() == folders[100]
    assert win.filter.currentData() == "GRID"
    win.close()


def test_coin_picker_filters_multiselects_and_keeps_previous_coins(app):
    from bcc.coin_picker import CoinPicker

    picker = CoinPicker("VELES", ["ETHUSDT"])
    picker.loaded(["BTCUSDT", "ETHUSDT", "SOLUSDT"], "")
    picker.search.setText("BTC")
    picker.select_visible()
    picker.add_selected()
    picker.commit()
    assert picker.result_symbols == ["ETHUSDT", "BTCUSDT"]
    assert picker.available.selectedItems()[0].text() == "BTCUSDT"
    picker.close()



def test_polymarket_coin_picker_has_offline_supported_catalog_and_preserves_selection(app):
    """The chooser must never be empty merely because Gamma API didn't respond."""
    from bcc.coin_picker import CoinPicker
    from bcc.market import COIN_NAMES

    picker = CoinPicker("POLYMARKET", ["BTC", "ETH"])
    try:
        assert {picker.available.item(i).text() for i in range(picker.available.count())} == set(COIN_NAMES)
        assert picker.chosen.count() == 2
        assert "перед PAPER-входом" in picker.notice.text()
        picker.search.setText("ZEC")
        picker.select_visible()
        picker.add_selected()
        picker.commit()
        assert picker.result_symbols == ["BTC", "ETH", "ZEC"]
    finally:
        picker.close()


def test_coin_picker_error_does_not_erase_selected_or_previously_loaded(app):
    from bcc.coin_picker import CoinPicker

    picker = CoinPicker("VELES", ["BTCUSDT"])
    try:
        picker.loaded(["BTCUSDT", "ETHUSDT"], "")
        picker.loaded([], "Bybit: HTTP 429")
        assert picker.available.count() == 2
        assert picker.chosen.count() == 1
        assert "Повторите загрузку" in picker.notice.text()
        picker.loaded(["SOLUSDT"], "")
        assert picker.available.count() == 1
        assert picker.chosen.item(0).text() == "BTCUSDT"
    finally:
        picker.close()


def test_bybit_coin_picker_starts_inventory_automatically_on_show(app, monkeypatch):
    from bcc.coin_picker import CoinPicker

    calls = []
    def fake_load(self):
        calls.append(self.strategy)
        self._inventory_requested = True
        self.loaded(["ETHUSDT"], "")

    monkeypatch.setattr(CoinPicker, "load", fake_load)
    picker = CoinPicker("VELES", ["BTCUSDT"])
    try:
        assert calls == []
        picker.show()
        for _ in range(10):
            app.processEvents()
        assert calls == ["VELES"]
        assert picker.available.item(0).text() == "ETHUSDT"
        assert picker.chosen.item(0).text() == "BTCUSDT"
    finally:
        picker.close()


def test_add_all_853_symbols_can_save_reopen_and_import_as_draft(
    app, tmp_path, monkeypatch
):
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import QFileDialog

    from bcc.coin_picker import CoinPicker
    from bcc.storage import atomic_json

    symbols = ["BTCUSDT", "ETHUSDT", "AUSDT", "币安人生USDT"] + [
        f"ASSET{i}USDT" for i in range(849)
    ]
    picker = CoinPicker("VELES", ["BTCUSDT", "ETHUSDT"])
    picker.loaded(symbols, "")
    picker.add_all()
    picker.commit()
    assert len(picker.result_symbols) == 853
    win = MainWindow(tmp_path / "application")
    cfg = defaults()
    cfg["name"] = "853 symbols"
    wizard = BotWizard(cfg, win)
    wizard.symbols.setText(", ".join(picker.result_symbols))
    wizard.show()
    wizard.steps.setCurrentRow(5)
    app.processEvents()
    QTest.mouseClick(wizard.save, Qt.MouseButton.LeftButton)
    assert wizard.result_config is not None, wizard.error.text()
    folder = win.repo.create(wizard.result_config)
    saved, digest = win.repo.load(folder)
    assert set(saved["symbols"]) == set(symbols)
    saved["name"] = "853 symbols edited"
    win.repo.save(folder, saved, digest)
    reopened = BotWizard(win.repo.load(folder)[0], win)
    assert len(reopened.data()["symbols"]) == 853
    reopened.close()
    draft = tmp_path / "old_package_draft.json"
    atomic_json(draft, saved)
    before = draft.read_bytes()
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", lambda *a, **kw: (str(draft), "JSON")
    )
    errors = []

    def save_draft():
        modal = app.activeModalWidget()
        try:
            assert isinstance(modal, BotWizard)
            assert len(modal.data()["symbols"]) == 853
            modal.name.setText("Recovered draft")
            modal.commit()
        except BaseException as exc:
            errors.append(exc)
            modal.reject()

    QTimer.singleShot(0, save_draft)
    win.open_draft()
    if errors:
        raise errors[0]
    assert len(win.repo.list()) == 2
    assert win.repo.load(win.current)[0]["name"] == "Recovered draft"
    assert draft.read_bytes() == before
    picker.close()
    wizard.close()
    win.close()


def test_canceling_new_section_does_not_create_it(app, monkeypatch):
    import bcc.widgets as widgets

    cfg = defaults()
    editor = widgets.SectionsEditor(cfg["sections"], cfg["settings"])
    monkeypatch.setattr(widgets, "confirm_dialog", lambda *a, **kw: False)
    editor.add()
    assert editor.value_data() == cfg["sections"]
    editor.close()


def test_secondary_indicator_has_its_own_editable_parameters(app, monkeypatch):
    from PySide6.QtWidgets import QSpinBox

    import bcc.widgets as widgets
    from bcc.config import condition, group, validate_group

    row = widgets.ConditionRow(condition())
    row.reference.setCurrentIndex(row.reference.findData("indicator"))
    row.ref_ind.setCurrentIndex(row.ref_ind.findData("MACD"))

    def fill_parameters(widget, *args):
        controls = widget.findChildren(QSpinBox)
        controls[0].setValue(50)
        controls[1].setValue(7)
        return True

    monkeypatch.setattr(widgets, "confirm_dialog", fill_parameters)
    row.reference_params_dialog()
    result = row.value_data()
    assert result["reference_params"] == {"slow": 50, "signal": 7}
    assert result["params"] == {}
    validate_group(group(result))
    row.close()


def test_invalid_modal_save_preserves_draft_and_allows_correction(app, monkeypatch):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QDialogButtonBox

    import bcc.widgets as widgets

    cfg = defaults()
    editor = widgets.SectionEditor(cfg["sections"][0], cfg["settings"])
    editor.name.setText("")
    warnings = []
    monkeypatch.setattr(
        widgets.QMessageBox, "warning", lambda *a: warnings.append(a[-1])
    )

    def interact():
        dialog = app.activeModalWidget()
        save = dialog.findChild(QDialogButtonBox).button(
            QDialogButtonBox.StandardButton.Save
        )
        save.click()
        assert dialog.isVisible() and warnings
        assert editor.name.text() == ""
        editor.name.setText("Corrected range")
        save.click()

    QTimer.singleShot(0, interact)
    assert widgets.confirm_dialog(editor, "Edit section")
    assert editor.value_data()["label"] == "Corrected range"


def test_coin_section_edit_preserves_inheritance_and_applies_individual_direction(
    app, monkeypatch
):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QCheckBox, QDialogButtonBox, QPushButton, QTabWidget

    import bcc.wizard as wizard_module
    from bcc.config import coin_section, effective

    cfg = defaults()
    cfg["name"] = "Inheritance"
    sec = cfg["sections"][0]
    sec["overrides"] = {"leverage": 4}
    cfg["coin_overrides"] = {
        "BTCUSDT": {"sections": {sec["id"]: {"settings": {"orders": {"count": 5}}}}}
    }
    wizard = BotWizard(cfg)
    errors = []

    def edit_child(child, *args, **kwargs):
        assert child.settings.value_data()["leverage"] == 4
        assert child.settings.value_data()["orders"]["count"] == 5
        child.override.setChecked(True)
        child.settings.orders.set_values({"count": 3})
        child.side.setCurrentIndex(child.side.findData("SHORT"))
        return True

    monkeypatch.setattr(wizard_module, "confirm_dialog", edit_child)

    def interact():
        modal = app.activeModalWidget()
        try:
            individual = modal.findChild(QCheckBox, "coinIndividual")
            assert individual.isChecked()
            modal.findChild(QPushButton, "editCoinSection").click()
            tabs = modal.findChild(QTabWidget, "coinSettingsTabs")
            individual.setChecked(False)
            assert not tabs.isEnabled()
            individual.setChecked(True)
            assert tabs.widget(0).isEnabled()
            modal.findChild(QDialogButtonBox).button(
                QDialogButtonBox.StandardButton.Save
            ).click()
        except BaseException as e:
            errors.append(e)
            modal.reject()

    QTimer.singleShot(0, interact)
    wizard.edit_coin()
    if errors:
        raise errors[0]
    result = wizard.data()
    patch = result["coin_overrides"]["BTCUSDT"]["sections"][sec["id"]]
    assert patch == {"settings": {"orders": {"count": 3}}, "side": "SHORT"}
    result["settings"]["fee_rate"] = 0.002
    result["sections"][0]["entry"]["conditions"][0]["value"] = 35
    assert effective(result, "BTCUSDT", result["sections"][0])["fee_rate"] == 0.002
    assert (
        coin_section(result, "BTCUSDT", result["sections"][0])["entry"]["conditions"][
            0
        ]["value"]
        == 35
    )
    validate(result)
    wizard.close()


def test_new_coin_parameters_can_be_enabled_and_applied_to_all(app):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QCheckBox, QDialogButtonBox, QPushButton, QTabWidget

    cfg = defaults("GRID")
    cfg["name"] = "Two grids"
    wizard = BotWizard(cfg)
    errors = []

    def interact():
        modal = app.activeModalWidget()
        try:
            individual = modal.findChild(QCheckBox, "coinIndividual")
            tabs = modal.findChild(QTabWidget, "coinSettingsTabs")
            assert not tabs.isEnabled()
            individual.setChecked(True)
            assert tabs.widget(0).isEnabled()
            finance = tabs.widget(0)
            finance.finance.set_values({"leverage": 8})
            next(
                b
                for b in modal.findChildren(QPushButton)
                if b.text() == "Применить текущие параметры ко всем монетам"
            ).click()
            modal.findChild(QDialogButtonBox).button(
                QDialogButtonBox.StandardButton.Save
            ).click()
        except BaseException as e:
            errors.append(e)
            modal.reject()

    QTimer.singleShot(0, interact)
    wizard.edit_coin()
    if errors:
        raise errors[0]
    cfg = wizard.data()
    assert cfg["coin_overrides"]["BTCUSDT"]["settings"] == {"leverage": 8}
    assert cfg["coin_overrides"]["ETHUSDT"]["settings"] == {"leverage": 8}
    validate(cfg)
    wizard.close()


def test_polymarket_combination_exposes_time_window_and_preserves_coin_defaults(
    app, monkeypatch
):
    from PySide6.QtWidgets import QInputDialog

    import bcc.wizard as wizard_module

    cfg = defaults("POLYMARKET")
    cfg["name"] = "Window selection"
    cfg["coin_overrides"] = {"BTC": {"parameters": {"ask_min": 0.2}}}
    wizard = BotWizard(cfg)
    monkeypatch.setattr(
        QInputDialog, "getItem", lambda *args, **kwargs: ("BTC:300", True)
    )

    def edit(fields, *args, **kwargs):
        assert fields.value_data()["ask_min"] == 0.2
        assert {
            "entry_signal_mode", "entry_seconds_before_close",
            "entry_window_seconds", "entry_min_seconds_left",
        } <= fields.controls.keys()
        fields.set_values(
            {
                "entry_signal_mode": "before_close",
                "entry_seconds_before_close": 40,
                "entry_window_seconds": 20,
                "entry_min_seconds_left": 8,
            }
        )
        kwargs["validator"]()
        return True

    monkeypatch.setattr(wizard_module, "confirm_dialog", edit)
    wizard.edit_poly_market()
    patch = wizard.data()["polymarket"]["overrides"]["BTC:300"]
    assert patch == {
        "entry_signal_mode": "before_close",
        "entry_seconds_before_close": 40,
        "entry_window_seconds": 20,
        "entry_min_seconds_left": 8,
    }
    validate(wizard.data())
    wizard.close()


def test_grid_presets_change_the_parameters_used_by_the_worker(app):
    from PySide6.QtWidgets import QPushButton

    cfg = defaults()
    editor = SectionEditor(cfg["sections"][0], cfg["settings"])
    editor.override.setChecked(True)
    preset = next(
        b
        for b in editor.settings.findChildren(QPushButton)
        if b.text().startswith("Консервативная")
    )
    preset.click()
    result = editor.value_data()
    cfg["sections"][0] = result
    from bcc.config import effective

    params = effective(cfg, "BTCUSDT", result)["orders"]
    assert (params["coverage_pct"], params["count"], params["martingale_pct"]) == (
        40,
        20,
        5,
    )
    editor.close()


def test_gui_created_multicoin_rsi_worker_completes_and_recovers(app, tmp_path):
    """Real widgets -> saved config -> production worker -> durable executions.

    Public transport and wall clock alone are replayed in the child process;
    strategy, RSI aggregation, executor, accounting and lifecycle are unchanged.
    """
    import math
    import time

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QDialogButtonBox
    from test_process import RUNNER, launch, wait_for, wait_for_position_count

    from bcc.indicators import evaluate
    from bcc.storage import Store, atomic_json

    win = MainWindow(tmp_path / "application")
    wizard = BotWizard(defaults(), win)
    wizard.show()
    wizard.name.setText("RSI two coins")
    wizard.steps.setCurrentRow(2)
    wizard.sections.table.selectRow(1)
    # VelesSectionsEditor.remove() intentionally requires a native QMessageBox
    # confirmation. The old acceptance test never answered it and blocked the
    # entire Windows regression at this precise testcase until the 300s timeout.
    from PySide6.QtWidgets import QDialog, QMessageBox

    modal_errors = []
    answered = []

    def close_unexpected_modals():
        modal_errors.append("Qt confirmation/edit did not complete within 5 seconds")
        # Fail closed rather than allowing a nested QMessageBox to block QA.
        # Reject nested dialogs from the top until the modal stack is clear.
        for widget in reversed(app.topLevelWidgets()):
            if isinstance(widget, QDialog) and widget.isModal() and widget.isVisible():
                widget.reject()

    modal_watchdog = QTimer(wizard)
    modal_watchdog.setSingleShot(True)
    modal_watchdog.timeout.connect(close_unexpected_modals)

    def confirm_remove():
        modal = app.activeModalWidget()
        if not isinstance(modal, QMessageBox):
            modal_errors.append("Expected native QMessageBox for deleting RSI range")
            if isinstance(modal, QDialog):
                modal.reject()
            return
        yes = modal.button(QMessageBox.StandardButton.Yes)
        if yes is None or not yes.isEnabled():
            modal_errors.append("Delete confirmation has no enabled Yes button")
            modal.reject()
            return
        answered.append(modal.windowTitle())
        yes.click()

    modal_watchdog.start(5000)
    QTimer.singleShot(0, confirm_remove)
    wizard.sections.remove()
    modal_watchdog.stop()
    assert not modal_errors, modal_errors
    assert len(answered) == 1, "Native delete confirmation was not exercised"
    assert len(wizard.sections.value_data()) == 1, "Confirmed range was not removed"
    wizard.sections.table.selectRow(0)
    errors = []

    def edit_section():
        modal = app.activeModalWidget()
        try:
            child = modal.findChild(SectionEditor)
            child.exit.rows[0].value.setValue(70)
            modal.findChild(QDialogButtonBox).button(
                QDialogButtonBox.StandardButton.Save
            ).click()
        except BaseException as e:
            errors.append(e)
            modal.reject()

    modal_watchdog.start(5000)
    QTimer.singleShot(0, edit_section)
    wizard.sections.edit()
    modal_watchdog.stop()
    assert not modal_errors, modal_errors
    if errors:
        raise errors[0]
    wizard.settings.orders.set_values({"count": 1})
    wizard.commit()
    assert wizard.result_config is not None
    win._created(wizard.result_config)
    folder = win.selected()
    cfg, _ = win.repo.load(folder)
    sec = cfg["sections"][0]
    assert sec["entry"]["conditions"][0]["timeframe"] == 2
    assert sec["exit"]["conditions"][0]["timeframe"] == 1
    base = int(time.time()) // 120 * 120000 - 148 * 60000
    prices = [100 + 6 * math.sin(i / 12) for i in range(148)]
    history = [
        dict(
            t=base + i * 60000,
            open=p,
            high=p + 0.2,
            low=p - 0.2,
            close=p,
            volume=100 + i % 4,
        )
        for i, p in enumerate(prices)
    ]
    frame = {
        "bars": history[:140],
        "price": prices[139],
        "now": (base + 140 * 60000) / 1000,
    }
    assert evaluate(sec["entry"], frame["bars"], int(frame["now"] * 1000), {}).matched
    assert evaluate(sec["exit"], history, base + 148 * 60000, {}).matched
    assert not evaluate(sec["entry"], history, base + 148 * 60000, {}).matched
    atomic_json(folder / "feed.json", frame)
    runner = tmp_path / "rsi_replay_worker.py"
    clock = """
from bcc.market_clock import clock as public_clock
from threading import Lock
replay_lock=Lock()
replay_anchor=[None, None]
def replay_bounds():
 path=Path(__import__('sys').argv[__import__('sys').argv.index('--folder')+1])
 stamp=json.loads((path/'feed.json').read_text(encoding='utf-8'))['now']
 with replay_lock:
  mono=time.monotonic()
  if replay_anchor[0] != stamp:
   replay_anchor[:]=[stamp, mono]
  current=stamp + mono-replay_anchor[1]
  return current,current
public_clock._attempted=0
public_clock._reference=(0,0,0,0)
public_clock.bounds=replay_bounds
"""
    runner.write_text(
        RUNNER.replace("worker.main()", clock + "\nworker.main()"), encoding="utf-8"
    )
    child = launch(win.life, folder, runner)
    try:
        wait_for_position_count(folder, child, 2)
        win.refresh_details()
        assert win.positions.rowCount() == 2
        assert win.history.rowCount() == 2
        assert win.life.active_snapshot(folder)["active_config"]["sections"][0] == sec
        cfg_bytes = (folder / "config.json").read_bytes()
        changed = dict(cfg, name="External disk change")
        atomic_json(folder / "config.json", changed)
        changed_bytes = (folder / "config.json").read_bytes()
        documents, source = win.settings_documents(folder)
        assert documents["Действует в процессе"]["name"] == cfg["name"]
        assert documents["На диске"]["name"] == changed["name"]
        assert "отличается" in source
        assert (folder / "config.json").read_bytes() == changed_bytes
        atomic_json(folder / "config.json", cfg)
        assert (folder / "config.json").read_bytes() == cfg_bytes
        win.life.finish(folder)
        wait_for(lambda: Store(folder).read()["strategy"]["draining"])
        assert win.settings_documents(folder)[0]["Управление выполнением"]["draining"]
        frame.update(bars=history, price=prices[-1], now=(base + 148 * 60000) / 1000)
        atomic_json(folder / "feed.json", frame)
        wait_for(lambda: not Store(folder).read()["positions"])
        child.wait(timeout=7)
        assert child.returncode == 0
        win.refresh_details()
        assert win.positions.rowCount() == 0
        assert win.history.rowCount() == 4
        assert win.sections.rowCount() == 2
        before = Store(folder).read()
        executions = Store(folder).history()
        assert sorted(e["action"] for e in executions) == [
            "CLOSE",
            "CLOSE",
            "OPEN",
            "OPEN",
        ]
        assert {e["symbol"] for e in executions} == {"BTCUSDT", "ETHUSDT"}
        assert before["cash"] > 500
        Store(folder).audit()
        frame["now"] += 1
        atomic_json(folder / "feed.json", frame)
        child = launch(win.life, folder, runner)
        wait_for(
            lambda: all(
                frame["now"] <= Store(folder).read()["marks"].get(s, {}).get("at", 0) < frame["now"] + 10
                for s in cfg["symbols"]
            )
        )
        win.life.stop(folder)
        child.wait(timeout=7)
        assert Store(folder).read()["cash"] == before["cash"]
        assert len(Store(folder).history()) == len(executions)
        Store(folder).audit()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=7)
        wizard.close()
        win.close()


def test_settings_report_includes_saved_pending_grid_after_settings_change(
    app, tmp_path
):
    from test_core import attach, bars

    from bcc.config import condition, group
    from bcc.storage import Store
    from bcc.strategies import Veles

    cfg = defaults()
    cfg["name"] = "Pending grid settings"
    cfg["symbols"] = ["BTCUSDT"]
    cfg["sections"] = cfg["sections"][:1]
    cfg["sections"][0]["entry"] = group(condition("PRICE", 0, ">", 1))
    cfg["settings"]["orders"]["first_offset_pct"] = 1
    window = MainWindow(tmp_path)
    folder = window.repo.create(cfg)
    store = Store(folder, cfg["capital"])
    try:
        with store.transaction() as (state, events):
            attach(state)
            Veles().process(
                cfg, state, events, "BTCUSDT", bars([100, 100]), 99.99, 100.01, 120000
            )
            assert state["pending"]
            state.pop("_books")
        edited, digest = window.repo.load(folder)
        edited["settings"]["orders"]["first_offset_pct"] = 5
        window.repo.save(folder, edited, digest)
        before = store.path.read_bytes()
        documents, _ = window.settings_documents(folder)
        assert documents["На диске"]["settings"]["orders"]["first_offset_pct"] == 5
        pending = next(iter(documents["Ожидающие сетки"].values()))
        assert pending["settings"]["orders"]["first_offset_pct"] == 1
        assert pending["exit_signal"] == cfg["sections"][0]["exit"]
        assert store.path.read_bytes() == before
    finally:
        window.close()


def test_settings_report_refresh_copy_and_export_use_current_safe_snapshot(
    app, tmp_path, monkeypatch
):
    import json

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QFileDialog, QPushButton

    from bcc.inspection import inspect_settings

    report_path = tmp_path / "safe_settings.json"
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", lambda *a, **kw: (str(report_path), "JSON")
    )
    initial = {"На диске": {"name": "Before", "apiKey": "sensitive-test-value"}}
    calls, errors = [], []
    old_clipboard = app.clipboard().text()

    def refresh():
        calls.append(True)
        return {
            "На диске": {"name": "After", "apiKey": "sensitive-test-value"}
        }, "Обновлённый снимок"

    def interact():
        modal = app.activeModalWidget()
        try:
            modal.findChild(QPushButton, "refreshSettingsReport").click()
            next(
                b
                for b in modal.findChildren(QPushButton)
                if b.text() == "Копировать безопасный отчёт"
            ).click()
            copied = json.loads(app.clipboard().text())
            assert copied["На диске"] == {"name": "After", "apiKey": "[скрыто]"}
            assert copied["Сведения об отчёте"]["Источник"] == "Обновлённый снимок"
            next(
                b
                for b in modal.findChildren(QPushButton)
                if b.text() == "Экспорт отчёта"
            ).click()
            assert json.loads(report_path.read_text(encoding="utf-8")) == copied
        except BaseException as e:
            errors.append(e)
        finally:
            modal.reject()

    QTimer.singleShot(0, interact)
    try:
        inspect_settings(None, initial, "Начальный снимок", refresh=refresh)
    finally:
        app.clipboard().setText(old_clipboard)
    if errors:
        raise errors[0]
    assert len(calls) == 1
    assert initial["На диске"]["apiKey"] == "sensitive-test-value"


def test_grid_preview_uses_selected_coin_section_side_capital_and_stop(app):
    cfg = defaults()
    cfg["settings"]["orders"].update(count=2, coverage_pct=15, martingale_pct=0)
    cfg["settings"]["stop"].update(mode="both", pct=5)
    section = cfg["sections"][0]
    cfg["coin_overrides"] = {
        "BTCUSDT": {
            "sections": {section["id"]: {"side": "SHORT", "settings": {"leverage": 4}}}
        }
    }
    wizard = BotWizard(cfg)
    wizard.steps.setCurrentRow(5)
    wizard.update_summary()
    assert "SHORT" in wizard.review.text()
    assert "Условный SL: 120" in wizard.review.text()
    assert float(wizard.preview.item(1, 1).text()) == 115
    assert float(wizard.preview.item(0, 3).text()) == 50
    wizard.preview_coin.setCurrentIndex(wizard.preview_coin.findData("ETHUSDT"))
    wizard.update_summary()
    assert "LONG" in wizard.review.text()
    assert "Условный SL: 80" in wizard.review.text()
    assert float(wizard.preview.item(1, 1).text()) == 85
    assert float(wizard.preview.item(0, 3).text()) == 37.5
    wizard.close()


def test_failed_stopped_bot_and_positions_remain_visible_without_filter(app, tmp_path):
    from test_core import config
    from test_execution_runtime import open_position

    from bcc.storage import Store, atomic_json

    win = MainWindow(tmp_path / "application")
    cfg = config()
    folder = win.repo.create(cfg)
    store = Store(folder, 500)
    with store.transaction() as (state, events):
        open_position(state, events, cfg["settings"])
        state.pop("_books")
    atomic_json(
        folder / "process.json", {"status": "ERROR", "last_error": "database is locked"}
    )
    try:
        win.refresh()
        assert "ошибкой" in win.bots_table.item(0, 3).text()
        win.show_running_bots()
        monitor = win.running_monitor
        monitor.refresh()
        assert not monitor.include_stopped.isChecked()
        wait_for_monitor(app, monitor, lambda: monitor.bots.rowCount() == 1
                         and monitor.positions.rowCount() == 1)
        assert "ошибкой" in monitor.bots.item(0, 3).text()
        assert "database is locked" in monitor.bots.item(0, 13).text()
        assert "database is locked" in monitor.alert.text()
        assert not monitor.alert.isHidden()
        assert "приостановлено" in monitor.positions.item(0, 9).text()
        assert "Работающих: 0" in monitor.summary.text()
        assert win.life.status(folder) == "Остановлен"  # Restart remains permitted.
    finally:
        if win.running_monitor:
            win.running_monitor.close()
        win.close()


def test_813_stale_quotes_do_not_collapse_bot_and_position_tables(app, tmp_path):
    from PySide6.QtWidgets import QSplitter
    from test_core import config
    from test_execution_runtime import open_position

    from bcc.storage import Store

    win = MainWindow(tmp_path / "application")
    cfg = config()
    cfg["symbols"] = ["BTCUSDT"] + [f"ASSET{i}USDT" for i in range(812)]
    folder = win.repo.create(cfg)
    store = Store(folder, 500)
    with store.transaction() as (state, events):
        for i in range(13):
            open_position(state, events, cfg["settings"], key=f"BTCUSDT:main{i}")
        state.pop("_books")
        state["marks"] = {
            s: {"bid": 100, "ask": 100.1, "at": 1} for s in cfg["symbols"]
        }
    try:
        win.current = folder
        win.resize(1000, 650)
        win.show()
        win.refresh()
        app.processEvents()
        assert win.positions.rowCount() == 13
        assert "813" in win.notice.text() and len(win.notice.text()) < 400
        assert win.notice.height() <= 90
        assert win.bots_table.viewport().height() >= 40
        assert win.positions.viewport().height() >= 60
        splitter = win.findChild(QSplitter, "botDetailsSplitter")
        assert not splitter.childrenCollapsible()
        assert min(splitter.sizes()) >= 100
    finally:
        win.close()


def test_current_report_button_uses_the_open_window_root(app, tmp_path, monkeypatch):
    import json
    import zipfile

    from PySide6.QtWidgets import QFileDialog, QMessageBox, QPushButton

    from bcc.config import defaults

    win = MainWindow(tmp_path / "actual_application")
    cfg = defaults("VELES")
    cfg["name"] = "Current window bot"
    folder = win.repo.create(cfg)
    target = tmp_path / "current_report.zip"
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", lambda *a, **kw: (str(target), "ZIP (*.zip)")
    )
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **kw: None)
    try:
        button = win.findChild(QPushButton, "saveCurrentBotsReport")
        assert button is not None
        button.click()
        with zipfile.ZipFile(target) as archive:
            summary = json.loads(
                archive.read("current_bots_report/report_summary.json")
            )
            assert summary["data_root"] == str(win.repo.root)
            assert summary["bot_count"] == 1 and summary["read_only"]
            assert any(folder.name in name for name in archive.namelist())
    finally:
        win.close()


def test_empty_window_report_can_read_existing_bot_root_without_import(
    app, tmp_path, monkeypatch
):
    import json
    import zipfile

    from PySide6.QtWidgets import QFileDialog, QMessageBox, QPushButton
    from test_core import config

    from bcc.storage import Repository

    old = Repository(tmp_path / "existing_application")
    folder = old.create(config())
    win = MainWindow(tmp_path / "new_empty_application")
    target = tmp_path / "existing_report.zip"
    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", lambda *a, **kw: str(old.root)
    )
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", lambda *a, **kw: (str(target), "ZIP (*.zip)")
    )
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **kw: None)
    try:
        win.findChild(QPushButton, "saveCurrentBotsReport").click()
        with zipfile.ZipFile(target) as archive:
            summary = json.loads(
                archive.read("current_bots_report/report_summary.json")
            )
            assert summary["data_root"] == str(old.root) and summary["bot_count"] == 1
            assert any(folder.name in name for name in archive.namelist())
        assert not win.repo.list()  # Reporting does not import or create a copy.
    finally:
        win.close()


def test_poly_seconds_matrix_sort_and_unchanged_precision(app):
    from PySide6.QtCore import Qt

    config = defaults("POLYMARKET")
    config["name"] = "Schedule precision"
    config["polymarket"].update(
        entry_signal_mode="before_close",
        entry_seconds_before_close=45.123456789012,
        entry_window_seconds=10.123456789012,
        entry_min_seconds_left=5.123456789012,
    )
    wizard = BotWizard(config)
    assert wizard.data()["polymarket"]["entry_seconds_before_close"] == 45.123456789012
    assert wizard.data()["polymarket"]["entry_window_seconds"] == 10.123456789012
    matrix = wizard.poly_matrix
    assert matrix.rowCount() == len(config["symbols"]) * len(config["polymarket"]["durations"])
    matrix.sortItems(1, Qt.SortOrder.DescendingOrder)
    durations = [float(matrix.item(i, 1).text()) for i in range(matrix.rowCount())]
    assert durations == sorted(durations, reverse=True)
    first = matrix.item(0, 0)
    assert first.data(Qt.ItemDataRole.UserRole + 1)
    config_out = validate(wizard.data())
    assert config_out["polymarket"]["entry_seconds_before_close"] == 45.123456789012
    wizard.close()


def test_polymarket_actual_entry_evidence_visible_without_start_or_config_mutation(
    app, tmp_path
):
    """Modern Qt reads frozen PAPER execution facts, not the edited disk rule."""
    from bcc.storage import Store
    from bcc.worker import process_polymarket
    from test_poly_contract_seconds import ScheduledSource, cfg as poly_cfg

    cfg = poly_cfg()
    cfg['symbols'] = ['BTC']
    cfg['polymarket']['durations'] = [300]
    win = MainWindow(tmp_path / 'app')
    folder = win.repo.create(cfg)
    store = Store(folder, cfg['capital'])
    feed = ScheduledSource(42)
    with store.transaction() as (state, events):
        process_polymarket(cfg, state, events, feed, feed.now)
    before = store.read()['positions']
    try:
        edited, digest = win.repo.load(folder)
        edited['polymarket']['entry_seconds_before_close'] = 70
        win.repo.save(folder, edited, digest)
        documents, source = win.settings_documents(folder)
        frozen = next(iter(documents['Открытые позиции'].values()))
        assert frozen['entry_rule']['entry_seconds_before_close'] == 45
        assert frozen['entry_observation']['observed_ask'] == .5
        assert documents['На диске']['polymarket']['entry_seconds_before_close'] == 70
        assert store.read()['positions'] == before
    finally:
        win.close()


def test_constructor_does_not_close_or_lose_values_after_create_write_failure(app, tmp_path):
    """The Qt editor remains usable when MY_BOTS cannot be written."""
    from bcc.storage import Repository
    from PySide6.QtWidgets import QDialog

    repo = Repository(tmp_path / "application")
    cfg = defaults("POLYMARKET")
    cfg["name"] = "Retry creation"
    attempts = []

    def persist(proposed):
        attempts.append(proposed["polymarket"]["entry_seconds_before_close"])
        if len(attempts) == 1:
            raise OSError("simulated write failure")
        persist.created = repo.create(proposed)
        return True

    draft = tmp_path / "draft_POLYMARKET.json"
    wizard = BotWizard(cfg, draft_path=draft, on_save=persist)
    wizard.show()
    app.processEvents()
    wizard.steps.setCurrentRow(2)
    time_field = wizard.poly.controls["entry_seconds_before_close"][1]
    time_field.setValue(41)
    wizard.commit()
    assert wizard.isVisible()
    assert wizard.result_config is None
    assert "simulated write failure" in wizard.error.text()
    assert "черновик сохранён" in wizard.error.text()
    from bcc.storage import load_json
    assert load_json(draft)["polymarket"]["entry_seconds_before_close"] == 41
    assert time_field.value() == 41
    assert not repo.list()
    wizard.commit()
    assert wizard.result() == QDialog.DialogCode.Accepted
    saved, _ = repo.load(persist.created)
    assert saved["polymarket"]["entry_seconds_before_close"] == 41
    assert attempts == [41, 41]
    wizard.close()


def test_constructor_edit_cancel_and_storage_conflict_preserve_draft(app, tmp_path):
    """Declining diff or a stale optimistic digest must not discard the editor."""
    from bcc.storage import Repository
    from PySide6.QtWidgets import QDialog

    repo = Repository(tmp_path / "application")
    cfg = defaults("VELES")
    cfg["name"] = "Conflict editing"
    folder = repo.create(cfg)
    existing, digest = repo.load(folder)
    cancelled = {"value": True}

    def persist(proposed):
        if cancelled["value"]:
            return False  # user declined changes review
        repo.save(folder, proposed, digest)
        return True

    wizard = BotWizard(existing, on_save=persist)
    wizard.show()
    app.processEvents()
    wizard.name.setText("Unsaved title")
    wizard.commit()
    assert wizard.isVisible() and wizard.result_config is None
    assert repo.load(folder)[0]["name"] == "Conflict editing"

    # A second Dashboard changed the on-disk config while this editor was open.
    newer, current_digest = repo.load(folder)
    newer["name"] = "Other dashboard"
    repo.save(folder, newer, current_digest)
    cancelled["value"] = False
    wizard.commit()
    assert wizard.isVisible() and wizard.result_config is None
    assert "изменились" in wizard.error.text()
    assert wizard.name.text() == "Unsaved title"
    assert repo.load(folder)[0]["name"] == "Other dashboard"
    wizard.reject()
    assert wizard.result() == QDialog.DialogCode.Rejected


def test_modern_strategy_picker_routes_only_real_strategy_keys(app):
    from bcc.config import STRATEGIES
    from bcc.strategy_picker import StrategyPicker
    from PySide6.QtWidgets import QDialog, QPushButton, QScrollArea
    from PySide6.QtCore import Qt

    for strategy in STRATEGIES:
        picker = StrategyPicker()
        picker.show()
        app.processEvents()
        buttons = picker.findChildren(QPushButton, "strategyChoose")
        assert len(buttons) == len(STRATEGIES)
        choice = next(b for b in buttons if b.property("strategy") == strategy)
        picker.findChild(QScrollArea).ensureWidgetVisible(choice)
        app.processEvents()
        QTest.mouseClick(choice, Qt.MouseButton.LeftButton)
        assert picker.result() == QDialog.DialogCode.Accepted
        assert picker.selected_strategy == strategy
        picker.close()


def test_dashboard_action_controls_fit_available_screen_without_clipping(app, tmp_path):
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QPushButton
    win = MainWindow(tmp_path / 'responsive_dashboard')
    try:
        win.show()
        app.processEvents()
        available = app.primaryScreen().availableGeometry()
        assert win.width() <= available.width()
        assert win.height() <= available.height()
        area = win.findChild(__import__('PySide6.QtWidgets', fromlist=['QFrame']).QFrame,
                             'dashboardActionArea')
        controls = area.findChildren(QPushButton)
        assert len(controls) == 12
        assert {'▶ Запуск', '■ Стоп', 'Общий риск', 'Редактировать',
                'Монитор всех ботов', 'Импорт / экспорт'} <= {b.text() for b in controls}
        for control in controls:
            corner = control.mapTo(area, QPoint(0, 0))
            assert control.isVisible()
            assert 0 <= corner.x() and corner.x() + control.width() <= area.width()
            assert 0 <= corner.y() and corner.y() + control.height() <= area.height()
    finally:
        win.close()
