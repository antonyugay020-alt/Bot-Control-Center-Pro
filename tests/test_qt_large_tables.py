"""Native Qt table acceptance for stable high-cardinality positions.

This test must execute on actual PySide6/QTableWidget on Windows Qt QA.
"""
import os
import pytest

if os.name != 'nt' and not os.environ.get('DISPLAY'):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
pytest.importorskip('PySide6')
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from bcc.ui import populate, table


def test_thousands_of_positions_update_without_recreating_all_cells():
    app = QApplication.instance() or QApplication([])
    t = table(['Symbol', 'Side', 'Unrealized'])
    try:
        count = 2500
        ids = [f'position-{i}' for i in range(count)]
        rows = [[f'COIN{i}USDT', 'LONG', '0.0000'] for i in range(count)]
        populate(t, rows, ids)
        assert t.rowCount() == count
        t.sortByColumn(0, Qt.SortOrder.DescendingOrder)
        selected = ids[101]
        current = next(r for r in range(t.rowCount())
                       if t.item(r, 0).data(Qt.ItemDataRole.UserRole) == selected)
        t.setCurrentCell(current, 0)
        t.selectRow(current)
        before = {t.item(r, 0).data(Qt.ItemDataRole.UserRole): t.item(r, 0)
                  for r in range(t.rowCount())}
        changes = {ids[0], ids[101], ids[-1]}
        updated = [[f'COIN{i}USDT', 'LONG', '1.5000' if ids[i] in changes else '0.0000']
                   for i in range(count)]
        populate(t, updated, ids)
        assert t.rowCount() == count
        after = {t.item(r, 0).data(Qt.ItemDataRole.UserRole): t.item(r, 0)
                 for r in range(t.rowCount())}
        assert set(after) == set(ids)
        assert all(before[key] is after[key] for key in ids)
        assert t.item(t.currentRow(), 0).data(Qt.ItemDataRole.UserRole) == selected
        for r in range(t.rowCount()):
            key = t.item(r, 0).data(Qt.ItemDataRole.UserRole)
            assert t.item(r, 2).text() == ('1.5000' if key in changes else '0.0000')
        # Adding a position must still use the safe rebuild path.
        populate(t, updated + [['EXTRAUSDT', 'SHORT', '0.0']], ids + ['extra'])
        assert t.rowCount() == count + 1
        assert any(t.item(r, 0).data(Qt.ItemDataRole.UserRole) == 'extra'
                   for r in range(t.rowCount()))
    finally:
        t.close()
