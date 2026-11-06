"""Native Windows Qt check of actual indicator-filter controls (no mock UI)."""
import os
if os.name != 'nt' and not os.environ.get('DISPLAY'):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtWidgets import QApplication
from bcc.config import defaults, validate
from bcc.wizard import BotWizard


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


def test_filter_turned_on_saved_reopened_and_disabled(app):
    cfg = defaults('POLYMARKET')
    cfg['name'] = 'Composite widget test'
    w = BotWizard(cfg)
    w.steps.setCurrentRow(2)
    assert w.poly_indicator_enabled.isChecked() is False
    assert w.data()['polymarket']['indicator_filter'] is None
    w.poly_indicator_enabled.setChecked(True)
    assert w.poly_indicator_editor.isEnabled()
    value = validate(w.data())
    assert value['polymarket']['indicator_filter']['conditions'][0]['indicator'] == 'RSI'
    w.close()
    reopened = BotWizard(value)
    assert reopened.poly_indicator_enabled.isChecked()
    assert reopened.data()['polymarket']['indicator_filter'] == value['polymarket']['indicator_filter']
    reopened.poly_indicator_enabled.setChecked(False)
    assert validate(reopened.data())['polymarket']['indicator_filter'] is None
    reopened.close()


def test_market_editor_reopens_and_preserves_filter_on_ask_edit(app, monkeypatch):
    """Native Qt regression: market GroupEditor is not a Fields control."""
    from bcc.config import condition, group, resolve_poly_market
    from bcc.storage import Repository
    from PySide6.QtWidgets import QCheckBox
    from bcc import wizard as wizard_module
    from tempfile import TemporaryDirectory
    from pathlib import Path

    cfg = defaults('POLYMARKET')
    cfg['name'] = 'Reopen and preserve filter'
    cfg['polymarket']['indicator_filter'] = group(condition('RSI', 45, '<', 1))
    specific = group(condition('EMA', 15, '>', 1))
    cfg['polymarket']['overrides']['BTC:300'] = {'indicator_filter': specific}
    w = BotWizard(cfg)

    def accept_ask_edit(fields, title, parent, validator=None):
        assert title.endswith('BTC:300')
        boxes = [box for box in fields.findChildren(QCheckBox)
                 if 'Индикаторные фильтры только этого рынка' in box.text()]
        assert len(boxes) == 1 and boxes[0].isChecked()
        fields.controls['ask_min'][1].setValue(.35)
        if validator is not None:
            validator()
        return True

    monkeypatch.setattr(wizard_module, 'confirm_dialog', accept_ask_edit)
    w.edit_poly_market('BTC:300')
    assert resolve_poly_market(w.data(), 'BTC', 300)['indicator_filter'] == specific
    assert resolve_poly_market(w.data(), 'BTC', 300)['ask_min'] == .35
    assert resolve_poly_market(w.data(), 'BTC', 900)['indicator_filter'] == cfg['polymarket']['indicator_filter']
    with TemporaryDirectory() as folder:
        repo = Repository(Path(folder))
        bot_id = repo.create(validate(w.data()))
        loaded, _ = repo.load(bot_id)
        assert resolve_poly_market(loaded, 'BTC', 300)['indicator_filter'] == specific
    w.close()


def test_market_filter_nested_group_removed_in_real_editor(app, monkeypatch):
    """Windows Qt: removing a nested branch must survive save and reopen."""
    from bcc.config import condition, group, resolve_poly_market
    from bcc.widgets import GroupEditor
    from PySide6.QtWidgets import QPushButton
    from bcc import wizard as wizard_module

    cfg = defaults('POLYMARKET')
    cfg['name'] = 'Remove nested filter branch'
    cfg['polymarket']['indicator_filter'] = {
        'operator': 'AND', 'conditions': [condition('RSI', 45, '<', 1)],
        'groups': [group(condition('EMA', 0, '>', 1))],
    }
    w = BotWizard(cfg)

    def accept_remove(fields, title, parent, validator=None):
        assert title.endswith('BTC:300')
        editor = next(editor for editor in fields.findChildren(GroupEditor)
                      if editor.groups)
        container, _ = editor.groups[0]
        remove = next(button for button in container.findChildren(QPushButton)
                      if button.text() == 'Удалить группу')
        remove.click()
        assert not editor.groups
        if validator is not None:
            validator()
        return True

    monkeypatch.setattr(wizard_module, 'confirm_dialog', accept_remove)
    w.edit_poly_market('BTC:300')
    edited = validate(w.data())
    assert 'groups' not in resolve_poly_market(edited, 'BTC', 300)['indicator_filter']
    assert 'groups' in resolve_poly_market(edited, 'BTC', 900)['indicator_filter']
    reopened = BotWizard(edited)
    assert 'groups' not in resolve_poly_market(reopened.data(), 'BTC', 300)['indicator_filter']
    reopened.close()
    w.close()
