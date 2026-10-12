"""Embeddable, offline daily candlestick chart for the StockShape desktop UI.

Public API: CandlestickChart.set_bars(frame, code='', name='', limit=250),
set_range(limit), and reset_view(). A limit of zero displays all available bars.
Prices use consecutive trading-bar indexes, so holidays create no empty gaps.
"""

from __future__ import annotations

import html
import math
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets


UP = "#e7505a"
DOWN = "#159b75"
MA_COLORS = {5: "#c79c32", 10: "#4285ce", 20: "#9971c5", 40: "#148e91"}
TEXT = "#334155"
MUTED = "#8792a2"
GRID = "#c7d0dc"


def _ensure_chinese_font() -> None:
    # The Windows offscreen Qt plugin may expose an empty font database.
    # Register installed fonts in memory so both previews and native UI work.
    if "Microsoft YaHei UI" in QtGui.QFontDatabase.families():
        return
    folder = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    for filename in ("msyh.ttc", "msyhbd.ttc"):
        path = folder / filename
        if path.is_file():
            QtGui.QFontDatabase.addApplicationFont(str(path))


class _TradingDateAxis(pg.AxisItem):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.dates: list[pd.Timestamp] = []

    def set_dates(self, dates) -> None:
        self.dates = list(dates)
        self.picture = None
        self.update()

    def tickStrings(self, values, scale, spacing):
        labels = []
        for value in values:
            index = int(round(value))
            if 0 <= index < len(self.dates) and abs(value - index) < 0.1:
                labels.append(self.dates[index].strftime("%Y-%m-%d"))
            else:
                labels.append("")
        return labels


class _VolumeAxis(pg.AxisItem):
    def tickStrings(self, values, scale, spacing):
        return [_compact_volume(value) for value in values]


class _ChartViewBox(pg.ViewBox):
    resetRequested = QtCore.Signal()

    def __init__(self):
        super().__init__(enableMenu=False)
        self.setMouseMode(self.PanMode)
        self.setMouseEnabled(x=True, y=False)

    def wheelEvent(self, event, axis=None):
        # Keep a financial-chart zoom horizontal. Y follows visible bars.
        super().wheelEvent(event, axis=0)

    def mouseDoubleClickEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.resetRequested.emit()
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)


class _CandleItem(pg.GraphicsObject):
    """One cached QPicture keeps long histories responsive during panning."""

    def __init__(self, rows: np.ndarray):
        super().__init__()
        self._picture = QtGui.QPicture()
        painter = QtGui.QPainter(self._picture)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, False)
        width = 0.64
        for x, opening, high, low, close in rows:
            color = QtGui.QColor(UP if close >= opening else DOWN)
            pen = QtGui.QPen(color)
            pen.setWidthF(0.9)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(color)
            painter.drawLine(QtCore.QPointF(x, low), QtCore.QPointF(x, high))
            if math.isclose(opening, close, abs_tol=1e-10):
                painter.drawLine(QtCore.QPointF(x - width / 2, close),
                                 QtCore.QPointF(x + width / 2, close))
            else:
                painter.drawRect(QtCore.QRectF(x - width / 2, min(opening, close),
                                              width, abs(close - opening)))
        painter.end()
        low = float(rows[:, 3].min())
        high = float(rows[:, 2].max())
        self._bounds = QtCore.QRectF(-0.6, low, len(rows) + 0.2,
                                    max(high - low, 0.001))

    def paint(self, painter, option, widget=None):
        painter.drawPicture(0, 0, self._picture)

    def boundingRect(self):
        return self._bounds


class _VolumeItem(pg.GraphicsObject):
    def __init__(self, volumes: np.ndarray, rising: np.ndarray):
        super().__init__()
        self._picture = QtGui.QPicture()
        painter = QtGui.QPainter(self._picture)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        for index, volume in enumerate(volumes):
            color = QtGui.QColor(UP if rising[index] else DOWN)
            color.setAlpha(175)
            painter.setBrush(color)
            painter.drawRect(QtCore.QRectF(index - 0.32, 0, 0.64, float(volume)))
        painter.end()
        self._bounds = QtCore.QRectF(-0.6, 0, len(volumes) + 0.2,
                                    max(float(volumes.max()), 1))

    def paint(self, painter, option, widget=None):
        painter.drawPicture(0, 0, self._picture)

    def boundingRect(self):
        return self._bounds


def _compact_volume(value: float) -> str:
    if not np.isfinite(value):
        return "—"
    if abs(value) >= 100_000_000:
        return f"{value / 100_000_000:.2f}亿"
    if abs(value) >= 10_000:
        return f"{value / 10_000:.1f}万"
    return f"{value:,.0f}"


class CandlestickChart(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        _ensure_chinese_font()
        self.setFont(QtGui.QFont("Microsoft YaHei UI", 9))
        self.setMinimumHeight(320)
        self._all_bars = pd.DataFrame()
        self._bars = pd.DataFrame()
        self._code = ""
        self._name = ""
        self._limit = 250
        self._candles = None
        self._volume_bars = None
        self._showing = False
        self._last_hover = None

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 10)
        layout.setSpacing(7)
        header = QtWidgets.QHBoxLayout()
        self.title_label = QtWidgets.QLabel("K线走势")
        self.title_label.setStyleSheet(f"color: {TEXT}; font-size: 14px; font-weight: 600;")
        self.range_label = QtWidgets.QLabel()
        self.range_label.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        self.reset_button = QtWidgets.QPushButton("重置视图")
        self.reset_button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.reset_button.setToolTip("恢复完整显示范围，也可以双击图表")
        self.reset_button.setStyleSheet(
            "QPushButton {color: #64748b; background: #f7f9fc; border: 1px solid #e2e8f0;"
            " border-radius: 6px; padding: 5px 10px; font-size: 11px;}"
            "QPushButton:hover {background: #edf2f8; color: #334155;}"
            "QPushButton:disabled {color: #b8c0cb;}"
        )
        self.reset_button.clicked.connect(self.reset_view)
        header.addWidget(self.title_label)
        header.addWidget(self.range_label)
        header.addStretch()
        header.addWidget(self.reset_button)
        layout.addLayout(header)

        self.details_label = QtWidgets.QLabel()
        self.details_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.details_label.setWordWrap(True)
        self.details_label.setMinimumHeight(42)
        self.details_label.setStyleSheet("color: #64748b; font-size: 11px; line-height: 150%;")
        layout.addWidget(self.details_label)
        self._stack = QtWidgets.QStackedWidget()
        layout.addWidget(self._stack, 1)
        self.canvas = pg.GraphicsLayoutWidget()
        self.canvas.setBackground("#ffffff")
        self.canvas.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.canvas.ci.layout.setContentsMargins(0, 0, 0, 0)
        self.canvas.ci.layout.setVerticalSpacing(7)
        self._stack.addWidget(self.canvas)
        self._date_axis = _TradingDateAxis(orientation="bottom")
        self._price_view = _ChartViewBox()
        self._volume_view = _ChartViewBox()
        self.price_plot = self.canvas.addPlot(row=0, col=0, viewBox=self._price_view)
        self.volume_plot = self.canvas.addPlot(
            row=1, col=0, viewBox=self._volume_view,
            axisItems={"bottom": self._date_axis, "left": _VolumeAxis(orientation="left")},
        )
        self.canvas.ci.layout.setRowStretchFactor(0, 4)
        self.canvas.ci.layout.setRowStretchFactor(1, 1)
        self.price_plot.hideAxis("bottom")
        self.volume_plot.setXLink(self.price_plot)
        self.volume_plot.setMinimumHeight(90)
        self._date_axis.setHeight(35)
        for plot in (self.price_plot, self.volume_plot):
            plot.setMenuEnabled(False)
            plot.hideButtons()
            plot.showGrid(x=False, y=True, alpha=0.18)
            plot.enableAutoRange(x=False, y=False)
            plot.getViewBox().setDefaultPadding(0)
            for orientation in ("left", "bottom"):
                axis = plot.getAxis(orientation)
                axis.setPen(pg.mkPen("#e2e8f0"))
                axis.setTextPen(pg.mkPen("#7c899a"))
                axis.setTickFont(QtGui.QFont("Microsoft YaHei UI", 8))
                axis.setStyle(tickTextOffset=7)
            plot.getAxis("left").setWidth(65)
        self._ma_items = {
            period: self.price_plot.plot(pen=pg.mkPen(color, width=1.25), connect="finite")
            for period, color in MA_COLORS.items()
        }
        crosshair_pen = pg.mkPen("#8492a6", width=0.8,
                                 style=QtCore.Qt.PenStyle.DashLine)
        self._price_vertical = pg.InfiniteLine(angle=90, pen=crosshair_pen, movable=False)
        self._price_horizontal = pg.InfiniteLine(angle=0, pen=crosshair_pen, movable=False)
        self._volume_vertical = pg.InfiniteLine(angle=90, pen=crosshair_pen, movable=False)
        self._volume_horizontal = pg.InfiniteLine(angle=0, pen=crosshair_pen, movable=False)
        for plot, items in (
            (self.price_plot, (self._price_vertical, self._price_horizontal)),
            (self.volume_plot, (self._volume_vertical, self._volume_horizontal)),
        ):
            for item in items:
                item.setZValue(20)
                plot.addItem(item, ignoreBounds=True)
                item.hide()
        self._price_view.sigXRangeChanged.connect(self._fit_visible_y)
        self._price_view.resetRequested.connect(self.reset_view)
        self._volume_view.resetRequested.connect(self.reset_view)
        self._mouse_proxy = pg.SignalProxy(self.canvas.scene().sigMouseMoved,
                                          rateLimit=45, slot=self._mouse_moved)

        empty = QtWidgets.QWidget()
        empty_layout = QtWidgets.QVBoxLayout(empty)
        empty_layout.addStretch()
        self.empty_title = QtWidgets.QLabel("暂无行情数据")
        self.empty_title.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.empty_title.setStyleSheet("color: #94a3b8; font-size: 17px; font-weight: 500;")
        self.empty_hint = QtWidgets.QLabel("从左侧选择一只股票，查看日K线与成交量。")
        self.empty_hint.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.empty_hint.setWordWrap(True)
        self.empty_hint.setStyleSheet("color: #a0aaba; font-size: 12px;")
        empty_layout.addWidget(self.empty_title)
        empty_layout.addWidget(self.empty_hint)
        empty_layout.addStretch()
        self._stack.addWidget(empty)
        self._show_empty()

    def set_bars(self, frame: pd.DataFrame, code: str = "", name: str = "",
                 limit: int = 250) -> None:
        """Set OHLCV data; calculate MA5/10/20/40 before applying the limit.

        Supported columns are date/open/high/low/close/volume (Chinese aliases
        and trade_date/vol are accepted). Volume may be absent. Input is copied.
        Change is calculated against the preceding supplied trading bar.
        """
        self._code, self._name = str(code or ""), str(name or "")
        self.title_label.setText(" ".join(x for x in (self._name, self._code) if x) or "K线走势")
        self._all_bars = pd.DataFrame()
        if frame is None or frame.empty:
            self._show_empty("本地暂未找到这只股票的行情，请先更新数据。")
            return
        aliases = {
            "date": ("date", "trade_date", "日期"),
            "open": ("open", "开盘"), "high": ("high", "最高"),
            "low": ("low", "最低"), "close": ("close", "收盘"),
            "volume": ("volume", "vol", "成交量"),
        }
        columns = {}
        for field, names in aliases.items():
            source = next((column for column in names if column in frame.columns), None)
            if source is None and field != "volume":
                self._show_empty(f"行情文件缺少 {field} 字段，暂时无法绘制。")
                return
            columns[field] = frame[source].copy() if source is not None else 0
        bars = pd.DataFrame(columns)
        bars["date"] = pd.to_datetime(bars["date"].astype(str), format="mixed", errors="coerce")
        for field in ("open", "high", "low", "close", "volume"):
            bars[field] = pd.to_numeric(bars[field], errors="coerce")
        bars = bars.replace([np.inf, -np.inf], np.nan)
        bars = bars.dropna(subset=["date", "open", "high", "low", "close"])
        bars = bars.loc[(bars[["open", "high", "low", "close"]] > 0).all(axis=1)]
        bars = bars.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
        if bars.empty:
            self._show_empty("行情文件中没有有效的日线记录。")
            return
        bars["volume"] = bars["volume"].fillna(0).clip(lower=0)
        bars["_change"] = bars["close"].pct_change(fill_method=None) * 100
        for period in MA_COLORS:
            bars[f"_ma{period}"] = bars["close"].rolling(period, min_periods=period).mean()
        self._all_bars = bars
        self.set_range(limit)

    def set_range(self, limit: int) -> None:
        """Show the latest N bars, or the full supplied history when limit=0."""
        self._limit = max(0, int(limit))
        if self._all_bars.empty:
            return
        self._showing = True
        try:
            self._bars = (self._all_bars.tail(self._limit) if self._limit
                          else self._all_bars).reset_index(drop=True)
            self._remove_price_items()
            count = len(self._bars)
            x = np.arange(count, dtype=float)
            rows = np.column_stack((x, self._bars[["open", "high", "low", "close"]].to_numpy()))
            self._candles = _CandleItem(rows)
            self._candles.setZValue(1)
            self.price_plot.addItem(self._candles)
            self._volume_bars = _VolumeItem(self._bars["volume"].to_numpy(),
                                            (self._bars["close"] >= self._bars["open"]).to_numpy())
            self.volume_plot.addItem(self._volume_bars)
            for period, item in self._ma_items.items():
                item.setZValue(3)
                item.setData(x=x, y=self._bars[f"_ma{period}"].to_numpy(), connect="finite")
            self._date_axis.set_dates(self._bars["date"])
            self.range_label.setText(
                f"日K · {count:,} 个交易日 · "
                f"{self._bars.iloc[0]['date']:%Y-%m-%d} — {self._bars.iloc[-1]['date']:%Y-%m-%d}"
            )
            self._stack.setCurrentIndex(0)
            self.reset_button.setEnabled(True)
            for view in (self._price_view, self._volume_view):
                view.setLimits(xMin=-1, xMax=count, minXRange=min(12, count + 1),
                               maxXRange=count + 2)
            self._last_hover = None
            self._hide_crosshair()
        finally:
            self._showing = False
        self.reset_view()
        self._update_details(len(self._bars) - 1)

    def reset_view(self) -> None:
        """Restore the full currently selected 120/250/all-bars display range."""
        if self._bars.empty:
            return
        self._price_view.setXRange(-0.8, len(self._bars) - 0.2, padding=0)
        self._fit_visible_y()

    def _remove_price_items(self):
        if self._candles is not None:
            self.price_plot.removeItem(self._candles)
            self._candles = None
        if self._volume_bars is not None:
            self.volume_plot.removeItem(self._volume_bars)
            self._volume_bars = None

    def _show_empty(self, hint: str = "从左侧选择一只股票，查看日K线与成交量。"):
        self._bars = pd.DataFrame()
        self._remove_price_items()
        for item in self._ma_items.values():
            item.setData([], [])
        self._hide_crosshair()
        self._date_axis.set_dates([])
        self.range_label.clear()
        self.details_label.setText("日K线 / MA5 · MA10 · MA20 · MA40 / 成交量")
        self.empty_hint.setText(hint)
        self.reset_button.setEnabled(False)
        self._stack.setCurrentIndex(1)

    def _fit_visible_y(self, *_):
        if self._showing or self._bars.empty:
            return
        left, right = self._price_view.viewRange()[0]
        start = max(0, math.floor(left))
        end = min(len(self._bars), math.ceil(right) + 1)
        if end <= start:
            return
        visible = self._bars.iloc[start:end]
        low, high = float(visible["low"].min()), float(visible["high"].max())
        for period in MA_COLORS:
            values = visible[f"_ma{period}"].dropna()
            if not values.empty:
                low, high = min(low, float(values.min())), max(high, float(values.max()))
        margin = max((high - low) * 0.065, high * 0.005, 0.02)
        self._price_view.setYRange(low - margin, high + margin, padding=0)
        self._volume_view.setYRange(0, max(float(visible["volume"].max()) * 1.18, 1), padding=0)

    def _mouse_moved(self, event):
        if self._bars.empty:
            return
        position = event[0]
        price_hover = self._price_view.sceneBoundingRect().contains(position)
        volume_hover = self._volume_view.sceneBoundingRect().contains(position)
        if not price_hover and not volume_hover:
            self._hide_crosshair()
            return
        view = self._price_view if price_hover else self._volume_view
        point = view.mapSceneToView(position)
        index = int(round(point.x()))
        if not 0 <= index < len(self._bars):
            self._hide_crosshair()
            return
        for line in (self._price_vertical, self._volume_vertical):
            line.setPos(index)
            line.show()
        self._price_horizontal.setVisible(price_hover)
        self._volume_horizontal.setVisible(volume_hover)
        (self._price_horizontal if price_hover else self._volume_horizontal).setPos(point.y())
        if index != self._last_hover:
            self._update_details(index)
            self._last_hover = index

    def _update_details(self, index: int):
        row = self._bars.iloc[index]
        change = row["_change"]
        color = UP if pd.notna(change) and change >= 0 else DOWN
        change_text = f"{change:+.2f}%" if pd.notna(change) else "—"
        identity = html.escape(" ".join(x for x in (self._code, self._name) if x))
        first_line = (
            f'<b style="color:{TEXT}">{identity}</b> &nbsp; '
            f'{row["date"]:%Y-%m-%d} &nbsp; '
            f'<span style="color:{color}">涨跌 {change_text}</span>'
            f' &nbsp; 成交量 {_compact_volume(float(row["volume"]))}'
        )
        prices = " &nbsp; ".join(
            f'{label} <span style="color:{TEXT}">{row[field]:.2f}</span>'
            for field, label in (("open", "开"), ("high", "高"), ("low", "低"), ("close", "收"))
        )
        average_parts = []
        for period, ma_color in MA_COLORS.items():
            value = row[f"_ma{period}"]
            value_text = f"{value:.2f}" if pd.notna(value) else "—"
            average_parts.append(
                f'<span style="color:{ma_color}">MA{period} {value_text}</span>'
            )
        averages = " &nbsp; ".join(average_parts)
        self.details_label.setText(first_line + "<br>" + prices + " &nbsp; " + averages)

    def _hide_crosshair(self):
        for line in (self._price_vertical, self._price_horizontal,
                     self._volume_vertical, self._volume_horizontal):
            line.hide()

    def leaveEvent(self, event):
        self._hide_crosshair()
        super().leaveEvent(event)
