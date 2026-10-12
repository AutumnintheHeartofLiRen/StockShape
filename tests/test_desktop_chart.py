"""Offline chart tests using synthetic trading bars; no market data or network."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pandas as pd
from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QApplication

from desktop.chart import CandlestickChart


def trading_bars(count: int) -> pd.DataFrame:
    index = np.arange(count, dtype=float)
    close = 35 + index * 0.31 + np.sin(index / 3) * 1.4
    return pd.DataFrame({
        "date": pd.bdate_range("2025-01-06", periods=count),
        "open": close - 0.35,
        "high": close + 0.8,
        "low": close - 0.9,
        "close": close,
        "volume": 100_000 + index * 1000,
    })


class CandlestickChartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.chart = CandlestickChart()
        self.chart.resize(1040, 680)
        self.chart.show()
        self.app.processEvents()

    def tearDown(self):
        self.chart.close()
        self.chart.deleteLater()
        self.app.processEvents()

    def hover_bar(self, index: int) -> None:
        self.app.processEvents()
        close = float(self.chart._bars.iloc[index]["close"])
        scene_position = self.chart._price_view.mapViewToScene(QPointF(index, close))
        self.chart._mouse_moved((scene_position,))

    def test_ma40_uses_history_before_the_display_window(self):
        frame = trading_bars(100)
        original = frame.copy(deep=True)
        self.chart.set_bars(frame, "000001", "离线样本", limit=12)
        # First visible bar is source index 88: its forty-bar average must
        # include indexes 49..88, despite only twelve displayed bars.
        expected_first = sum(frame["close"].iloc[49:89]) / 40
        self.assertEqual(len(self.chart._bars), 12)
        self.assertAlmostEqual(self.chart._bars.iloc[0]["_ma40"], expected_first)
        _, curve_values = self.chart._ma_items[40].getData()
        self.assertAlmostEqual(curve_values[0], expected_first)
        self.assertTrue(np.isfinite(curve_values).all())
        pd.testing.assert_frame_equal(frame, original)

    def test_ma40_requires_forty_actual_bars(self):
        self.chart.set_bars(trading_bars(39), limit=0)
        self.assertTrue(self.chart._bars["_ma40"].isna().all())
        _, curve_values = self.chart._ma_items[40].getData()
        self.assertTrue(np.isnan(curve_values).all())
        self.assertIn("MA40 —", self.chart.details_label.text())

        frame = trading_bars(40)
        self.chart.set_bars(frame, limit=0)
        self.assertTrue(self.chart._bars["_ma40"].iloc[:39].isna().all())
        expected = sum(frame["close"]) / 40
        self.assertAlmostEqual(self.chart._bars.iloc[39]["_ma40"], expected)
        self.assertEqual(self.chart._bars["_ma40"].notna().sum(), 1)

    def test_hover_shows_ma40_value_and_missing_value(self):
        frame = trading_bars(65)
        self.chart.set_bars(frame, "000001", "离线样本", limit=0)
        self.hover_bar(45)
        expected = sum(frame["close"].iloc[6:46]) / 40
        text = self.chart.details_label.text()
        self.assertIn(f"MA40 {expected:.2f}", text)
        self.assertIn("000001 离线样本", text)
        self.assertIn(f"{frame.iloc[45]['date']:%Y-%m-%d}", text)
        for period in (5, 10, 20):
            self.assertIn(f"MA{period} ", text)
        self.assertTrue(self.chart._price_vertical.isVisible())
        self.assertTrue(self.chart._volume_vertical.isVisible())
        color = self.chart._ma_items[40].opts["pen"].color().name()
        self.assertEqual(color, "#148e91")
        self.assertNotIn(color, [self.chart._ma_items[p].opts["pen"].color().name()
                                 for p in (5, 10, 20)])
        self.hover_bar(38)
        self.assertIn("MA40 —", self.chart.details_label.text())

    def test_range_changes_retain_ma40_and_linked_trading_indexes(self):
        frame = trading_bars(320)
        self.chart.set_bars(frame, limit=250)
        for limit, count in ((120, 120), (250, 250), (0, 320)):
            with self.subTest(limit=limit):
                self.chart.set_range(limit)
                self.app.processEvents()
                self.assertEqual(len(self.chart._bars), count)
                self.assertEqual(self.chart._bars.iloc[0]["date"], frame.iloc[-count]["date"])
                curve_x, curve_values = self.chart._ma_items[40].getData()
                np.testing.assert_array_equal(curve_x, np.arange(count))
                self.assertAlmostEqual(curve_values[-1], sum(frame["close"].iloc[-40:]) / 40)
                if limit:
                    first_index = len(frame) - count
                    expected_first = sum(frame["close"].iloc[first_index - 39:first_index + 1]) / 40
                    self.assertAlmostEqual(curve_values[0], expected_first)
                else:
                    self.assertTrue(np.isnan(curve_values[:39]).all())
                np.testing.assert_allclose(self.chart._price_view.viewRange()[0],
                                           self.chart._volume_view.viewRange()[0])

    def test_ma40_is_visible_when_above_recent_candles(self):
        frame = trading_bars(100)
        frame["close"] = 200 - np.arange(100) * 1.2
        frame["open"] = frame["close"] + 0.2
        frame["high"] = frame["close"] + 0.4
        frame["low"] = frame["close"] - 0.4
        self.chart.set_bars(frame, limit=0)
        self.chart._price_view.setXRange(75, 80, padding=0)
        self.app.processEvents()
        expected_ma40 = sum(frame["close"].iloc[36:76]) / 40
        self.assertGreater(expected_ma40, frame.iloc[75]["high"])
        low, high = self.chart._price_view.viewRange()[1]
        self.assertLess(low, expected_ma40)
        self.assertGreater(high, expected_ma40)

    def test_empty_state_mentions_ma40_and_clears_old_curves(self):
        self.assertIn("MA40", self.chart.details_label.text())
        self.chart.set_bars(trading_bars(80), limit=120)
        self.assertEqual(self.chart._stack.currentIndex(), 0)
        self.chart.set_bars(pd.DataFrame())
        self.assertEqual(self.chart._stack.currentIndex(), 1)
        self.assertIn("暂无行情数据", self.chart.empty_title.text())
        self.assertIn("MA40", self.chart.details_label.text())
        self.assertFalse(self.chart.reset_button.isEnabled())
        curve_x, curve_values = self.chart._ma_items[40].getData()
        self.assertTrue(curve_x is None or len(curve_x) == 0)
        self.assertTrue(curve_values is None or len(curve_values) == 0)
        self.chart.set_range(0)
        self.chart.reset_view()
        self.assertEqual(self.chart._stack.currentIndex(), 1)


if __name__ == "__main__":
    unittest.main()
