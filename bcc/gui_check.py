"""Capture and check the actual Qt interface in an isolated, empty bot workspace."""

import argparse
import os
import platform
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--offscreen",
        action="store_true",
        help="Development rendering; never native Windows acceptance",
    )
    args = parser.parse_args()
    if args.offscreen:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6.QtCore import QCoreApplication, QEvent, QTimer
    from PySide6.QtWidgets import QApplication, QDialog

    from .coin_picker import CoinPicker
    from .config import STRATEGIES, defaults, validate
    from .storage import Store, atomic_json
    from .ui import MainWindow, apply_style
    from .wizard import BotWizard

    args.output.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    apply_style(app, {"theme": "dark", "scale": 100})
    report = {
        "platform": platform.platform(),
        "qt_platform": app.platformName(),
        "native_windows": os.name == "nt" and app.platformName() == "windows",
        "checks": [],
        "test_data": "Isolated bot configurations; no fabricated market data or trades",
    }
    temp = tempfile.TemporaryDirectory(prefix="bcc-gui-check-")
    window = MainWindow(Path(temp.name))
    for kind in STRATEGIES:
        cfg = defaults(kind)
        cfg["name"] = "Проверка · " + kind
        window.supervisor.create_bot(cfg)
    window.refresh()
    window.show()
    created_by_strategy = {}

    def capture():
        try:
            available = app.primaryScreen().availableGeometry()
            report["screen"] = {
                "width": available.width(),
                "height": available.height(),
            }
            if (
                window.width() > available.width()
                or window.height() > available.height()
            ):
                raise ValueError("Главное окно не помещается на экране")
            window.grab().save(str(args.output / "dashboard.png"))
            for kind in STRATEGIES:
                cfg = defaults(kind)
                cfg["name"] = "Проверка · " + kind
                # Exercise the *actual* constructor save callback and repository
                # persistence, not just a read-only serialization of widgets.
                created = []
                def persist_created(proposed):
                    created.append(window.supervisor.create_bot(proposed))
                    return True
                wizard = BotWizard(cfg, window, on_save=persist_created)
                try:
                    wizard.show()
                    for page in range(wizard.pages.count()):
                        wizard.steps.setCurrentRow(page)
                        app.processEvents()
                        validate(wizard.data())
                        if page == 1 and not wizard.finance_widget.isVisible():
                            raise ValueError(kind + ": скрыты финансовые настройки")
                        if (
                            page == 3
                            and kind
                            in ("VELES", "EMA", "MACD", "BOLLINGER", "EMA_RSI_VOLUME")
                            and not wizard.settings.orders.isVisible()
                        ):
                            raise ValueError(kind + ": скрыты параметры сетки")
                        if kind == "VELES" and page in (1, 3):
                            wizard.grab().save(
                                str(
                                    args.output
                                    / (
                                        "veles_finance.png"
                                        if page == 1
                                        else "veles_orders.png"
                                    )
                                )
                            )
                        if wizard.data() != cfg:
                            raise ValueError(
                                kind + ": редактор изменил незатронутые параметры"
                            )
                    wizard.steps.setCurrentRow(2)
                    app.processEvents()
                    if (
                        wizard.width() > available.width()
                        or wizard.height() > available.height()
                    ):
                        raise ValueError(kind + ": конструктор не помещается на экране")
                    wizard.grab().save(str(args.output / (kind.lower() + ".png")))
                    # 1. New bot: modify a real field, commit through the GUI,
                    #    then reopen the persisted configuration from MY_BOTS.
                    wizard.name.setText(cfg["name"] + " · сохранено")
                    expected_created = validate(wizard.data())
                    wizard.commit()
                    if (len(created) != 1 or wizard.result_config != expected_created
                            or wizard.result() != QDialog.DialogCode.Accepted):
                        raise ValueError(kind + ": конструктор не сохранил нового бота")
                    persisted, digest = window.repo.load(created[0])
                    if persisted != expected_created:
                        raise ValueError(kind + ": сохранённая конфигурация не совпадает с формой")
                    # 2. Edit an initialized PAPER account through the real Qt
                    # constructor; a name-only edit never exercises funding.
                    Store(created[0], persisted["capital"])
                    original_capital = persisted["capital"]
                    def persist_edited(proposed):
                        window.supervisor.save_bot(created[0], proposed, digest)
                        return True
                    editor = BotWizard(persisted, window, on_save=persist_edited)
                    try:
                        editor.name.setText(persisted["name"] + " · редактировано")
                        editor.capital.setValue(original_capital + 25)
                        expected_edited = validate(editor.data())
                        editor.commit()
                        edited, _ = window.repo.load(created[0])
                        if (editor.result() != QDialog.DialogCode.Accepted or
                                editor.result_config != expected_edited or
                                edited != expected_edited):
                            raise ValueError(kind + ": редактирование не сохранено на диске")
                        ledger = Store(created[0], expected_edited["capital"])
                        state = ledger.read()
                        if (state["initial"] != original_capital
                                or abs(state["cash"] - (original_capital + 25)) > 1e-7
                                or state["realized"] != 0
                                or len(ledger.history()) != 1
                                or ledger.history()[0]["action"] != "CAPITAL_ADJUST"):
                            raise ValueError(kind + ": изменение депозита через Qt не отражено в PAPER-счёте")
                        ledger.audit()
                    finally:
                        editor.close()
                        editor.deleteLater()
                    created_by_strategy[kind] = created[0]
                    report["checks"].append(
                        {"strategy": kind, "pages": wizard.pages.count(),
                         "created_and_reopened": "PASS", "edited_and_reopened": "PASS",
                         "deposit_adjustment_with_ledger": "PASS", "status": "PASS"}
                    )
                finally:
                    wizard.close()
                    wizard.deleteLater()
            # Run the full persistent operator lifecycle on the exact bots
            # created and edited by the visible Qt wizard, not synthetic files.
            # No network trades are manufactured: this checks the command and
            # ledger boundary; real PAPER fills are tested separately on Windows.
            for kind in ("VELES", "GRID", "PUMP_DUMP", "POLYMARKET"):
                source = created_by_strategy[kind]
                source_state = Store(source).read()
                original_events = Store(source).history()
                copy = window.supervisor.copy_bot(source, history=True)
                if copy == source or Store(copy).read()["cash"] != source_state["cash"]:
                    raise ValueError(kind + ": копия не сохранила независимый PAPER-счёт")
                if Store(copy).history() != original_events:
                    raise ValueError(kind + ": при копировании потеряна история")

                export_path = args.output / (kind.lower() + "_transfer.zip")
                window.supervisor.export_bots([source], export_path, history=True)
                imported = window.supervisor.import_bots(export_path, history=True)
                if len(imported) != 1 or imported[0] in (source, copy):
                    raise ValueError(kind + ": импорт создал не независимую копию")
                if (Store(imported[0]).read()["cash"] != source_state["cash"]
                        or Store(imported[0]).history() != original_events):
                    raise ValueError(kind + ": импорт изменил PAPER-историю")

                csv_path = args.output / (kind.lower() + "_history.csv")
                window.supervisor.export_history_csv(source, csv_path)
                if not csv_path.is_file() or csv_path.stat().st_size == 0:
                    raise ValueError(kind + ": CSV-история не экспортирована")

                key = window.supervisor._key(source)
                start = window.supervisor.submit(source, running=True)
                active = window.supervisor.coordinator.get_registration(key)
                if (start.desired != "RUNNING" or active["desired"] != "RUNNING"
                        or not window.supervisor.coordinator.launch_allowed(
                            key, active["command_version"]
                        )):
                    raise ValueError(kind + ": START не зарегистрирован")
                stop = window.supervisor.submit(source, running=False)
                stopped = window.supervisor.coordinator.get_registration(key)
                if (stop.desired != "STOPPED" or stopped["desired"] != "STOPPED"
                        or window.supervisor.coordinator.launch_allowed(
                            key, active["command_version"]
                        )):
                    raise ValueError(kind + ": STOP не отозвал запуск")
                window.supervisor.delete_bot(source, discard_paper_state=True)
                if source.exists() or window.supervisor.coordinator.get_registration(key):
                    raise ValueError(kind + ": удаление не завершилось")
                if not copy.exists() or not imported[0].exists():
                    raise ValueError(kind + ": удаление затронуло независимые копии")
                Store(copy).audit()
                Store(imported[0]).audit()
                report.setdefault("operator_cycles", []).append({
                    "strategy": kind, "copy": "PASS", "transfer": "PASS",
                    "csv_history": "PASS", "command_revoke": "PASS",
                    "delete_and_copy_isolation": "PASS",
                    "paper_ledger_audit": "PASS",
                    "command_scope": "INTENT_ONLY_NOT_WORKER_E2E",
                })
            window.refresh()
            app.processEvents()
            window.show_running_bots()
            monitor = window.running_monitor
            monitor.include_stopped.setChecked(True)
            monitor.refresh()
            # Snapshot runs outside the GUI thread. A screenshot taken before
            # the worker finishes cannot establish that fleet monitoring works.
            import time
            deadline = time.monotonic() + 10.0
            while (monitor._loading or not monitor.records) and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.02)
            if monitor._loading or not monitor.records:
                raise ValueError("Монитор: фоновый снимок не завершился за 10 секунд")
            present = {row["config"].get("strategy") for row in monitor.records}
            if not {"VELES", "GRID", "PUMP_DUMP", "POLYMARKET"}.issubset(present):
                raise ValueError("Монитор: не отображены независимые боты основных стратегий")
            if monitor.alert.isVisible():
                raise ValueError("Монитор: ошибка фонового обновления: " + monitor.alert.text())
            if monitor.bots.rowCount() != len(monitor.records):
                raise ValueError("Монитор: число строк Qt не совпадает со снимком")
            if not monitor.grab().save(str(args.output / "all_bots_monitor.png")):
                raise ValueError("Монитор: не удалось сохранить снимок Qt")
            report["all_bots_monitor"] = "PASS"
            report["all_bots_monitor_rows"] = len(monitor.records)
            report["all_bots_monitor_strategies"] = sorted(present)
            monitor.close()
            report["coin_picker"] = []
            # Regression: Polymarket's selection list must not require a live
            # Gamma scan (the old 2,000-market scan often returned nothing).
            from .market import COIN_NAMES
            poly_picker = CoinPicker("POLYMARKET", ["BTC", "ETH"], window)
            try:
                poly_picker.show()
                app.processEvents()
                shown = {poly_picker.available.item(i).text()
                         for i in range(poly_picker.available.count())}
                if shown != set(COIN_NAMES):
                    raise ValueError("Polymarket: неполный список поддерживаемых монет")
                if poly_picker.chosen.count() != 2:
                    raise ValueError("Polymarket: потеряны ранее выбранные монеты")
                poly_picker.grab().save(str(args.output / "coin_picker_polymarket.png"))
                report["poly_coin_catalog"] = "PASS"
            finally:
                poly_picker.close()
                poly_picker.deleteLater()
            from PySide6.QtGui import QPalette

            def luminance(color):
                channels = [color.redF(), color.greenF(), color.blueF()]
                channels = [
                    x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4
                    for x in channels
                ]
                return sum(
                    x * weight for x, weight in zip(channels, (0.2126, 0.7152, 0.0722))
                )

            for theme in ("dark", "light"):
                apply_style(app, {"theme": theme, "scale": 100})
                picker = CoinPicker("VELES", ["ETHUSDT", "BTCUSDT"], window)
                try:
                    picker.notice.setText(
                        "Проверка контраста: подписи монет; список биржи не загружался."
                    )
                    # Fixed fixture for a visual contrast check. Mark it loaded
                    # so automatic live Bybit loading cannot replace the rows
                    # while the screenshot is being taken.
                    picker.loaded(["BTCUSDT", "ETHUSDT", "SOLUSDT"], "")
                    picker.available.setCurrentRow(0)
                    picker.chosen.setCurrentRow(0)
                    picker.show()
                    picker.available.setFocus()
                    app.processEvents()
                    minimum = 100.0
                    for view in (picker.available, picker.chosen):
                        palette = view.palette()
                        for group in (
                            QPalette.ColorGroup.Active,
                            QPalette.ColorGroup.Inactive,
                        ):
                            for foreground, background in (
                                (QPalette.ColorRole.Text, QPalette.ColorRole.Base),
                                (
                                    QPalette.ColorRole.HighlightedText,
                                    QPalette.ColorRole.Highlight,
                                ),
                            ):
                                a, b = sorted(
                                    (
                                        luminance(palette.color(group, foreground)),
                                        luminance(palette.color(group, background)),
                                    )
                                )
                                minimum = min(minimum, (b + 0.05) / (a + 0.05))
                    if minimum < 4.5:
                        raise ValueError(
                            f"Выбор монет, {theme}: недостаточный контраст {minimum:.2f}"
                        )
                    picker.grab().save(
                        str(args.output / ("coin_picker_" + theme + ".png"))
                    )
                    report["coin_picker"].append(
                        {
                            "theme": theme,
                            "minimum_contrast": round(minimum, 2),
                            "status": "PASS",
                        }
                    )
                finally:
                    picker.close()
                    picker.deleteLater()
            apply_style(app, {"theme": "dark", "scale": 100})
            report["status"] = "PASS"
        except Exception as e:
            report.update(status="FAIL", error=str(e))
        finally:
            atomic_json(args.output / "gui_report.json", report)
            window.close()
            window.deleteLater()
            QTimer.singleShot(0, app.quit)

    QTimer.singleShot(200, capture)
    app.exec()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    temp.cleanup()
    print(
        "Qt:",
        report["qt_platform"],
        report.get("status"),
        report.get("error", ""),
        flush=True,
    )
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
