"""Regression: bot-settings view must not inherit Windows white viewport + pale text."""

import os
if os.name != "nt" and not os.environ.get("DISPLAY"):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication
from bcc.inspection import tree
from bcc.ui import apply_style


def _luminance(color):
    parts = [color.redF(), color.greenF(), color.blueF()]
    linear = [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in parts]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(one, two):
    a, b = sorted((_luminance(one), _luminance(two)))
    return (b + 0.05) / (a + 0.05)


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_settings_tree_legible_after_theme_switch(theme):
    app = QApplication.instance() or QApplication([])
    apply_style(app, {"scale": 100, "theme": theme})
    view = tree({"Название": "Тестовый бот", "Торговые параметры": {"Монеты": ["BTCUSDT", "ETHUSDT"]}})
    view.show()
    app.processEvents()
    bg = view.viewport().palette().color(QPalette.ColorRole.Base)
    fg = view.palette().color(QPalette.ColorRole.Text)
    assert _contrast(bg, fg) >= 4.5, (theme, bg.name(), fg.name())
    assert view.topLevelItemCount() == 2
    assert view.topLevelItem(1).childCount() == 1
    view.close()
    view.deleteLater()
