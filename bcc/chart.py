"""Optional chart drawn from actual worker candles, never placeholder prices."""

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

from .indicators import series


class CandleChart(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.bars = []
        self.symbol = ""
        self.setMinimumHeight(260)

    def set_data(self, symbol, bars):
        self.symbol = symbol
        self.bars = bars[-120:]
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("#111e2d"))
        p.setPen(QColor("#b8cadf"))
        if not self.bars:
            p.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                "График появится после получения настоящих свечей",
            )
            return
        p.drawText(18, 24, self.symbol + " · свечи 1m · RSI(14)")
        left = 18
        right = self.width() - 90
        top = 40
        bottom = self.height() * 0.65
        low = min(b["low"] for b in self.bars)
        high = max(b["high"] for b in self.bars)
        span = max(high - low, high * 0.0001)

        def y(v):
            return bottom - (v - low) / span * (bottom - top)

        width = (right - left) / len(self.bars)
        for i in range(5):
            yy = top + (bottom - top) * i / 4
            p.setPen(QColor("#26374a"))
            p.drawLine(QPointF(left, yy), QPointF(right, yy))
            p.setPen(QColor("#9eb3cb"))
            p.drawText(right + 7, int(yy + 4), f"{high - span * i / 4:.6g}")
        for i, b in enumerate(self.bars):
            x = left + (i + 0.5) * width
            color = QColor("#2dd2b7" if b["close"] >= b["open"] else "#f17882")
            p.setPen(QPen(color, 1))
            p.drawLine(QPointF(x, y(b["high"])), QPointF(x, y(b["low"])))
            p.fillRect(
                QRectF(
                    x - width * 0.32,
                    min(y(b["open"]), y(b["close"])),
                    max(1, width * 0.64),
                    max(1, abs(y(b["open"]) - y(b["close"]))),
                ),
                color,
            )
        rsi = series("RSI", self.bars, 14)
        rtop = bottom + 25
        rbottom = self.height() - 22
        for level in (30, 50, 70):
            yy = rbottom - level / 100 * (rbottom - rtop)
            p.setPen(QColor("#344258"))
            p.drawLine(QPointF(left, yy), QPointF(right, yy))
            p.drawText(right + 7, int(yy + 4), str(level))
        p.setPen(QPen(QColor("#af9aee"), 1.5))
        previous = None
        for i, value in enumerate(rsi):
            if value is None:
                previous = None
                continue
            point = QPointF(
                left + (i + 0.5) * width, rbottom - value / 100 * (rbottom - rtop)
            )
            if previous is not None:
                p.drawLine(previous, point)
            previous = point
