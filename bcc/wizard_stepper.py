"""Interactive seven-step navigation rendered from real QListWidget selection.

Painting is passive: the selected page, configuration and validation remain
owned by BotWizard. A visited step is NOT presented as a trading-rule pass.
"""
from PySide6.QtCore import Qt, QRectF, QSize
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QApplication, QStyledItemDelegate, QStyle


class WizardStepDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        return QSize(230, 76)

    def paint(self, painter, option, index):
        details = index.data(Qt.ItemDataRole.UserRole) or {}
        title = details.get("title", "")
        subtitle = details.get("subtitle", "")
        visited = bool(details.get("visited", False))
        current = bool(option.state & QStyle.StateFlag.State_Selected)
        light = QApplication.instance().property("bccTheme") == "light"
        bg = QColor("#daf4ef" if light else "#123e4a") if current else QColor("#ffffff" if light else "#0e2235")
        strong = QColor("#067a72" if light else "#57ead9") if current else QColor("#17314b" if light else "#e9f5ff")
        muted = QColor("#58738b" if light else "#afc0d3")
        track = QColor("#9cc1cb" if light else "#35627a")
        teal = QColor("#10aa9c" if light else "#12d7c6")
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(option.rect).adjusted(2, 2, -3, -3)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg)
        painter.drawRoundedRect(rect, 9, 9)
        if current:
            painter.setBrush(teal)
            painter.drawRoundedRect(QRectF(rect.left(), rect.top()+5, 4, rect.height()-10), 2, 2)
        center_x = rect.left() + 28
        center_y = rect.top() + 26
        painter.setPen(QPen(track, 1.5))
        if index.row() > 0:
            painter.drawLine(int(center_x), int(rect.top()-3), int(center_x), int(center_y-19))
        if index.row() < index.model().rowCount()-1:
            painter.drawLine(int(center_x), int(center_y+19), int(center_x), int(rect.bottom()+4))
        circle = QRectF(center_x-17, center_y-17, 34, 34)
        painter.setPen(QPen(teal if current or visited else track, 1.7))
        painter.setBrush(teal if current else (QColor("#daf4ef" if light else "#123642") if visited else bg))
        painter.drawEllipse(circle)
        number_font = QFont(option.font)
        number_font.setPointSize(10)
        number_font.setBold(current or visited)
        painter.setFont(number_font)
        painter.setPen(QColor("#072b31") if current else (teal if visited else muted))
        painter.drawText(circle, Qt.AlignmentFlag.AlignCenter, str(index.row()+1))
        x = rect.left() + 53
        width = max(60, rect.width()-59)
        heading_font = QFont(option.font)
        heading_font.setPointSize(10)
        heading_font.setBold(current)
        painter.setFont(heading_font)
        painter.setPen(strong)
        painter.drawText(QRectF(x, rect.top()+12, width, 24),
                         int(Qt.AlignmentFlag.AlignVCenter) | int(Qt.TextFlag.TextSingleLine), title)
        sub_font = QFont(option.font)
        sub_font.setPointSize(8)
        painter.setFont(sub_font)
        painter.setPen(muted)
        painter.drawText(QRectF(x, rect.top()+35, width, 32),
                         int(Qt.AlignmentFlag.AlignTop) | int(Qt.TextFlag.TextWordWrap), subtitle)
        if visited and not current:
            painter.setPen(teal)
            painter.drawText(QRectF(rect.right()-20, rect.top()+7, 14, 14),
                             Qt.AlignmentFlag.AlignCenter, "✓")
        painter.restore()
