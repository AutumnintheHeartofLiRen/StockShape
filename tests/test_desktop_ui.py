"""Offline UI tests: temporary data only; background processes are simulated."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pandas as pd
from PySide6.QtCore import QByteArray, QObject, QProcess, Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from desktop.main import MainWindow
from desktop.services import Store
from start_desktop import project_root


class FakeProcess(QObject):
    """Exercise the real UI's signal wiring without executing a subprocess."""
    readyReadStandardOutput = Signal()
    readyReadStandardError = Signal()
    finished = Signal(int, object)
    errorOccurred = Signal(object)
    ProcessError = QProcess.ProcessError
    ExitStatus = QProcess.ExitStatus
    ProcessState = QProcess.ProcessState
    instances = []

    def __init__(self, parent=None):
        super().__init__(parent)
        self.output = b""
        self.errors = b""
        self.started = None
        self.killed = False
        self.deleted = False
        self.environment = None
        self.directory = None
        self.instances.append(self)

    def setProcessEnvironment(self, environment):
        self.environment = environment

    def setWorkingDirectory(self, directory):
        self.directory = directory

    def start(self, program, arguments):
        self.started = (program, arguments)

    def readAllStandardOutput(self):
        result, self.output = self.output, b""
        return QByteArray(result)

    def readAllStandardError(self):
        result, self.errors = self.errors, b""
        return QByteArray(result)

    def feed(self, data):
        self.output += data
        self.readyReadStandardOutput.emit()

    def errorString(self):
        return "simulated start failure"

    def kill(self):
        self.killed = True

    def waitForFinished(self, _timeout):
        return True

    def deleteLater(self):
        self.deleted = True
        super().deleteLater()


def result_row(code="000002", name="示例乙", selected=True):
    return {
        "code": code, "name": name, "date": "2026-09-30",
        "selected": selected, "verdict": "进入观察池" if selected else "未入选",
        "change_pct": 1.25, "reason": "离线测试形态结论", "status": "ok",
    }


class DesktopUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="stockshape_ui_")
        self.root = Path(self.temp.name)
        self.stocks = self.root / "data/full_a/stocks"
        self.stocks.mkdir(parents=True)
        # Synthetic settings and a placeholder provider; no real strategy is published.
        (self.root / "strategy_config.json").write_text(json.dumps({
            "version": 1, "rsi6_threshold": 50, "baseline_rsi6_threshold": 60,
            "previous_rsi6_threshold": 60, "changes": []}), encoding="utf-8")
        (self.root / "private").mkdir()
        (self.root / "private/strategy_engine.py").write_text("# UI task execution is simulated.\n", encoding="utf-8")
        for code, name, base in [
            ("000001", "示例甲银行", 10), ("000002", "示例乙", 20),
            ("600000", "示例丙银行", 30),
        ]:
            self.write_stock(code, name, base)
        pd.DataFrame([
            {"code": code, "new_last_date": "2026-09-30", "status": "updated", "error": ""}
            for code in ["000001", "000002", "600000"]
        ]).to_csv(self.root / "data/full_a/daily_update_status.csv", index=False)
        python = self.root / ".venv/Scripts/python.exe"
        python.parent.mkdir(parents=True)
        python.touch()  # FakeProcess prevents this placeholder being executed.
        FakeProcess.instances.clear()
        self.process_patch = mock.patch("desktop.main.QProcess", FakeProcess)
        self.process_patch.start()
        self.warning_patch = mock.patch("desktop.main.QMessageBox.warning", return_value=QMessageBox.StandardButton.Ok)
        self.warning = self.warning_patch.start()
        self.info_patch = mock.patch("desktop.main.QMessageBox.information", return_value=QMessageBox.StandardButton.Ok)
        self.info_patch.start()
        self.question_patch = mock.patch("desktop.main.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes)
        self.question_patch.start()
        self.windows = []
        self.window = self.new_window()

    def tearDown(self):
        for window in self.windows:
            # No fake task may cause a close confirmation or update guard.
            window.task = None
            window.process = None
            window.close()
            window.deleteLater()
        self.app.processEvents()
        self.question_patch.stop()
        self.info_patch.stop()
        self.warning_patch.stop()
        self.process_patch.stop()
        self.temp.cleanup()

    def write_stock(self, code, name, base=10):
        close = base + np.arange(600) * 0.01
        pd.DataFrame({
            "code": code, "name": name, "market": "主板",
            "date": pd.bdate_range(end="2026-09-30", periods=600),
            "open": close - .05, "high": close + .2, "low": close - .2,
            "close": close, "volume": 1000 + np.arange(600),
        }).to_csv(self.stocks / f"{code}.csv", index=False)

    def new_window(self):
        window = MainWindow(Store(self.root))
        self.windows.append(window)
        window.show()
        self.app.processEvents()
        return window

    def emit_result(self, process, rows, newline=True):
        message = json.dumps({"type": "result", "rows": rows,
                              "summary": {"date": "2026-09-30", "failed": 0}}, ensure_ascii=False)
        process.feed((message + ("\n" if newline else "")).encode("utf-8"))

    def test_search_by_code_and_name_and_clear(self):
        self.window.search.setText("000002")
        self.assertEqual(self.window.search_results.count(), 1)
        self.assertEqual(self.window.search_results.item(0).data(Qt.ItemDataRole.UserRole), "000002")
        self.window.search.setText("银行")
        self.assertEqual(self.window.search_results.count(), 2)
        self.window.search.setText("不存在")
        self.assertEqual(self.window.search_results.count(), 0)
        self.assertFalse(self.window.add_button.isEnabled())
        self.window.search.clear()
        self.assertEqual(self.window.search_results.count(), 0)

    def test_watchlist_add_deduplicate_remove_and_reload(self):
        self.window.search.setText("示例乙")
        self.window.add_button.click()
        self.window.add_button.click()
        self.assertEqual(Store(self.root).watchlist(), ["000002"])
        restarted = self.new_window()
        self.assertEqual(restarted.watch_list.count(), 1)
        self.assertEqual(restarted.watch_list.item(0).data(Qt.ItemDataRole.UserRole), "000002")
        restarted.watch_list.setCurrentRow(0)
        restarted.remove_button.click()
        self.assertEqual(Store(self.root).watchlist(), [])
        self.window.refresh_watchlist()
        self.assertEqual(self.window.watch_list.count(), 0)
        self.assertFalse(self.window.watch_button.isEnabled())

    def test_search_click_loads_stock_and_chart_ranges(self):
        self.window.search.setText("示例乙")
        item = self.window.search_results.item(0)
        QTest.mouseClick(self.window.search_results.viewport(), Qt.MouseButton.LeftButton,
                         pos=self.window.search_results.visualItemRect(item).center())
        self.assertEqual(self.window.current_code, "000002")
        self.assertIn("示例乙", self.window.stock_title.text())
        self.assertEqual(self.window.price_label.text(), "25.99")
        self.assertEqual(len(self.window.chart._bars), 250)
        for index, expected in [(0, 120), (2, 500), (3, 600), (1, 250)]:
            with self.subTest(range=index):
                self.window.range_combo.setCurrentIndex(index)
                self.assertEqual(len(self.window.chart._bars), expected)
                self.assertEqual(self.window.chart._bars.iloc[-1]["date"], pd.Timestamp("2026-09-30"))
        # Moving averages use the supplied full history before range slicing.
        self.assertFalse(pd.isna(self.window.chart._bars.iloc[0]["_ma20"]))
        self.assertFalse(pd.isna(self.window.chart._bars.iloc[0]["_ma40"]))

    def test_results_filter_and_table_selection_open_correct_stock(self):
        self.window.all_rows = [result_row(), result_row("600000", "示例丙银行", False)]
        self.window.refresh_all_table()
        self.assertEqual(self.window.all_table.rowCount(), 1)
        self.window.only_selected.setChecked(False)
        self.assertEqual(self.window.all_table.rowCount(), 2)
        self.window.result_filter.setText("示例丙")
        self.assertEqual(self.window.all_table.rowCount(), 1)
        self.window.all_table.selectRow(0)
        self.assertEqual(self.window.current_code, "600000")
        self.assertIn("示例丙银行", self.window.stock_title.text())

    def test_saved_analysis_is_loaded_by_a_new_window(self):
        self.window.store.add_watch("000002")
        self.window.store.user_dir.mkdir(exist_ok=True)
        for scope, rows in [("watchlist", [result_row()]),
                            ("all", [result_row(), result_row("600000", "示例丙银行", False)])]:
            (self.window.store.user_dir / f"analysis_{scope}.json").write_text(
                json.dumps({"rows": rows}, ensure_ascii=False), encoding="utf-8")
        restarted = self.new_window()
        self.assertEqual(len(restarted.all_rows), 2)
        self.assertEqual(restarted.watch_rows["000002"]["verdict"], "进入观察池")
        restarted.show_stock("000002")
        self.assertIn("离线测试形态结论", restarted.reason_label.text())
        self.assertEqual(restarted.all_threshold, 60)
        self.assertEqual(restarted.watch_threshold, 60)
        self.assertIn("旧参数", restarted.reason_label.text())
        self.assertIn("RSI6 < 60", restarted.all_summary.text())

    def test_start_task_is_exclusive_and_disables_mutations(self):
        self.window.store.add_watch("000002")
        self.window.refresh_watchlist()
        self.window.search.setText("示例甲")
        self.window.start_task("watchlist")
        process = self.window.process
        self.assertEqual(process.started[1][-1], "watchlist")
        self.assertEqual(process.environment.value("STOCKSHAPE_ROOT"), str(self.root))
        self.assertFalse(self.window.update_button.isEnabled())
        self.assertFalse(self.window.add_button.isEnabled())
        self.assertFalse(self.window.remove_button.isEnabled())
        self.assertFalse(self.window.rsi_combo.isEnabled())
        self.window.start_task("all")
        self.window.add_watch("000001")
        self.assertEqual(len(FakeProcess.instances), 1)
        self.assertEqual(self.window.store.watchlist(), ["000002"])

    def test_empty_watchlist_does_not_start_worker(self):
        self.window.start_task("watchlist")
        self.assertEqual(FakeProcess.instances, [])
        self.assertIsNone(self.window.task)
        self.assertIn("加入自选", self.window.status_label.text())

    def test_launcher_honours_explicit_project_root(self):
        with mock.patch.dict(os.environ, {"STOCKSHAPE_ROOT": str(self.root)}):
            self.assertEqual(project_root(), self.root.resolve())

    def test_utf8_progress_can_span_process_chunks(self):
        self.window.start_task("all")
        message = json.dumps({"type": "progress", "current": 1, "total": 3,
                              "message": "中文进度"}, ensure_ascii=False).encode("utf-8") + b"\n"
        cut = message.index("中".encode("utf-8")) + 1
        self.window.process.feed(message[:cut])
        self.window.process.feed(message[cut:])
        self.assertIn("中文进度", self.window.status_label.text())
        self.assertEqual(self.window.progress_bar.maximum(), 3)
        self.assertEqual(self.window.progress_bar.value(), 1)

    def test_result_without_final_newline_is_drained_on_exit(self):
        self.window.start_task("all")
        process = self.window.process
        self.emit_result(process, [result_row()], newline=False)
        self.assertFalse(self.window.received_result)
        process.finished.emit(0, QProcess.ExitStatus.NormalExit)
        self.assertEqual(self.window.all_rows[0]["code"], "000002")
        self.assertEqual(self.window.tabs.currentIndex(), 1)
        self.assertTrue(self.window.received_result)
        self.assertIsNone(self.window.task)
        self.assertIsNone(self.window.process)
        self.assertTrue(process.deleted)
        self.assertTrue(self.window.update_button.isEnabled())
        self.assertEqual(self.window.progress_bar.value(), self.window.progress_bar.maximum())
        self.assertEqual(self.window.task_percent.text(), "100.0%")
        self.warning.assert_not_called()

    def test_watchlist_result_updates_only_watchlist_view(self):
        self.window.store.add_watch("000002")
        self.window.refresh_watchlist()
        self.window.all_rows = [result_row("600000", "示例丙银行")]
        self.window.start_task("watchlist")
        process = self.window.process
        self.emit_result(process, [result_row()])
        process.finished.emit(0, QProcess.ExitStatus.NormalExit)
        self.assertEqual(self.window.watch_rows["000002"]["verdict"], "进入观察池")
        self.assertEqual(self.window.all_rows[0]["code"], "600000")
        self.assertEqual(self.window.tabs.currentIndex(), 0)

    def test_exit_without_result_reports_error_and_preserves_previous_rows(self):
        original = [result_row()]
        self.window.all_rows = original
        self.window.start_task("all")
        process = self.window.process
        process.feed(b'{"type":"error","message":"offline failure"}\n')
        process.finished.emit(1, QProcess.ExitStatus.NormalExit)
        self.assertEqual(self.window.all_rows, original)
        self.assertIn("offline failure", self.window.status_label.text())
        self.warning.assert_called_once()
        self.assertIsNone(self.window.task)
        self.assertIsNone(self.window.process)

    def test_failed_to_start_cleans_process_and_unlocks_controls(self):
        self.window.start_task("all")
        process = self.window.process
        process.errorOccurred.emit(QProcess.ProcessError.FailedToStart)
        self.assertIsNone(self.window.task)
        self.assertIsNone(self.window.process)
        self.assertTrue(process.deleted)
        self.assertTrue(self.window.update_button.isEnabled())
        self.warning.assert_called_once()

    def test_cancel_analysis_preserves_previous_rows(self):
        original = [result_row()]
        self.window.all_rows = original
        self.window.start_task("all")
        process = self.window.process
        self.window.cancel_task()
        self.assertTrue(process.killed)
        process.finished.emit(9, QProcess.ExitStatus.CrashExit)
        self.assertEqual(self.window.all_rows, original)
        self.assertIn("已停止", self.window.status_label.text())
        self.warning.assert_not_called()
        self.assertIsNone(self.window.task)

    def test_update_cannot_be_cancelled_or_closed_while_writing(self):
        self.window.start_task("update")
        process = self.window.process
        self.window.cancel_task()
        self.assertFalse(process.killed)
        event = QCloseEvent()
        self.window.closeEvent(event)
        self.assertFalse(event.isAccepted())
        self.assertFalse(process.killed)
        self.assertEqual(self.window.task, "update")

    def test_update_result_refreshes_cached_stock_metadata(self):
        self.window.start_task("update")
        process = self.window.process
        self.write_stock("300001", "新股票", 40)
        self.write_stock("000002", "更新名称", 20)
        self.emit_result(process, [])
        process.finished.emit(0, QProcess.ExitStatus.NormalExit)
        self.window.search.setText("新股票")
        self.assertEqual(self.window.search_results.count(), 1)
        self.assertEqual(self.window.names["000002"]["name"], "更新名称")
        self.assertIn("分析需单独运行", self.window.status_label.text())

    def test_threshold_restore_persists_and_labels_stale_results(self):
        self.assertEqual(self.window.rsi_combo.currentData(), 50)
        self.window.all_rows = [result_row()]
        self.window.all_threshold = 50
        self.window.show_stock("000002")
        self.window.rsi_combo.setCurrentIndex(self.window.rsi_combo.findData(60))
        self.assertEqual(Store(self.root).strategy()["rsi6_threshold"], 60)
        self.assertIn("旧参数", self.window.all_summary.text())
        self.assertIn("RSI6 < 50", self.window.reason_label.text())
        restarted = self.new_window()
        self.assertEqual(restarted.rsi_combo.currentData(), 60)
        self.assertIn("原值", restarted.rsi_combo.currentText())
        self.assertGreaterEqual(restarted.rsi_combo.findData(50), 0)
        self.window.rsi_combo.setCurrentIndex(self.window.rsi_combo.findData(50))
        self.assertNotIn("旧参数", self.window.all_summary.text())

    def test_result_keeps_its_recorded_strategy_threshold(self):
        self.window.start_task("all")
        self.window.handle_event_line(json.dumps({"type": "result", "rows": [result_row()],
            "summary": {"date": "2026-09-30", "rsi6_threshold": 50}}))
        self.assertEqual(self.window.all_threshold, 50)
        self.assertIn("RSI6 < 50", self.window.all_summary.text())
        self.assertNotIn("旧参数", self.window.all_summary.text())
        self.window.process.finished.emit(0, QProcess.ExitStatus.NormalExit)

    def test_custom_threshold_is_displayed_without_truncation(self):
        self.window.store.set_rsi6_threshold(50.5)
        restarted = self.new_window()
        self.assertEqual(restarted.rsi_combo.currentData(), 50.5)
        self.assertEqual(restarted.rsi_combo.currentText(), "50.5")
        self.assertEqual(MainWindow.result_threshold({"strategy": {"rsi6_threshold": 50.5}}), 50.5)
        self.assertIsNone(MainWindow.result_threshold({"strategy": None}))
        self.assertIsNone(MainWindow.result_threshold({"rsi6_threshold": "invalid"}))

    def test_missing_private_package_keeps_charts_and_watchlist_available(self):
        (self.root / "private/strategy_engine.py").unlink()
        (self.root / "strategy_config.json").unlink()
        restarted = self.new_window()
        self.assertFalse(restarted.all_button.isEnabled())
        self.assertFalse(restarted.rsi_combo.isEnabled())
        self.assertEqual(restarted.rsi_combo.currentText(), "未配置")
        restarted.search.setText("示例乙")
        restarted.add_button.click()
        self.assertEqual(restarted.store.watchlist(), ["000002"])
        self.assertFalse(restarted.watch_button.isEnabled())
        self.assertEqual(len(restarted.chart._bars), 250)
        restarted.start_task("all")
        self.assertIsNone(restarted.process)
        restarted.start_task("update")
        self.assertIsNotNone(restarted.process)

    def test_empty_public_install_starts_without_data_or_strategy(self):
        with tempfile.TemporaryDirectory() as directory:
            empty = MainWindow(Store(directory))
            self.windows.append(empty)
            empty.show()
            self.app.processEvents()
            self.assertFalse(empty.analysis_state["available"])
            self.assertEqual(empty.search_results.count(), 0)
            self.assertFalse(empty.all_button.isEnabled())
            self.assertIn("策略", empty.status_label.text())
            self.warning.assert_not_called()

    def test_update_panel_displays_preparation_then_current_stock_progress(self):
        self.assertFalse(self.window.task_panel.isVisible())
        self.window.start_task("update")
        self.app.processEvents()
        self.assertTrue(self.window.task_panel.isVisible())
        self.assertFalse(self.window.rsi_combo.isEnabled())
        self.window.handle_event_line(json.dumps({"type": "progress", "phase": "preparing",
            "current": 0, "total": 3, "message": "正在预检接口"}, ensure_ascii=False))
        self.assertEqual(self.window.progress_bar.value(), 0)
        self.assertEqual(self.window.task_percent.text(), "准备中")
        self.window.handle_event_line(json.dumps({"type": "progress", "phase": "updating",
            "current": 2, "total": 3, "code": "000002",
            "message": "[2/3] 000002 更新完成 | 失败:0"}, ensure_ascii=False))
        self.assertEqual(self.window.progress_bar.value(), 2)
        self.assertEqual(self.window.progress_bar.maximum(), 3)
        self.assertEqual(self.window.task_percent.text(), "66.7%")
        self.assertIn("000002", self.window.task_detail.text())
        self.assertIn("2/3", self.window.status_label.text())
        process = self.window.process
        self.emit_result(process, [])
        process.finished.emit(0, QProcess.ExitStatus.NormalExit)
        self.assertTrue(self.window.task_panel.isVisible())
        self.assertEqual(self.window.progress_bar.value(), 3)
        self.assertEqual(self.window.task_percent.text(), "100.0%")
        self.assertTrue(self.window.rsi_combo.isEnabled())

    def test_plain_worker_logs_are_visible(self):
        self.window.handle_event_line("updater legacy stdout")
        self.window.handle_event_line(json.dumps({"type": "log", "message": "本地日志"}, ensure_ascii=False))
        self.assertIn("updater legacy stdout", self.window.log_view.toPlainText())
        self.assertIn("本地日志", self.window.log_view.toPlainText())

    def test_invalid_protocol_shapes_do_not_interrupt_ui(self):
        for line in ["[]", "null", '"unexpected string"',
                     '{"type":"progress","current":"invalid","total":5}']:
            with self.subTest(line=line):
                self.window.handle_event_line(line)
        self.assertFalse(self.window.received_result)
        self.assertIn("忽略", self.window.log_view.toPlainText())

    def test_invalid_result_payloads_preserve_previous_analysis(self):
        original = [result_row()]
        self.window.all_rows = original
        self.window.start_task("all")
        process = self.window.process
        invalid = [
            {"rows": {}, "summary": {}},
            {"rows": [], "summary": []},
            {"rows": [None], "summary": {}},
            {"rows": [{}], "summary": {}},
            {"rows": [{"code": 779}], "summary": {}},
            {"rows": [{"code": "779"}], "summary": {}},
            {"rows": [{"code": "00077x"}], "summary": {}},
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                self.window.handle_event_line(json.dumps({"type": "result", **payload}))
                self.assertFalse(self.window.received_result)
                self.assertEqual(self.window.all_rows, original)
        process.finished.emit(0, QProcess.ExitStatus.NormalExit)
        self.warning.assert_called_once()
        self.assertIsNone(self.window.task)

    def test_cancelled_task_ignores_late_result_during_stdout_drain(self):
        original = [result_row()]
        self.window.all_rows = original
        self.window.start_task("all")
        process = self.window.process
        self.window.cancel_task()
        self.emit_result(process, [result_row("600000", "示例丙银行")], newline=False)
        process.finished.emit(9, QProcess.ExitStatus.CrashExit)
        self.assertEqual(self.window.all_rows, original)
        self.assertFalse(self.window.received_result)
        self.assertIn("已停止", self.window.status_label.text())
        self.warning.assert_not_called()
        self.assertIsNone(self.window.task)
        self.assertIsNone(self.window.process)


if __name__ == "__main__":
    unittest.main()
