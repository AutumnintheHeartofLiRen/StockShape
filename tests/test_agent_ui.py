"""Offline desktop Agent interaction; all processes and credentials are synthetic."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pandas as pd
from PySide6.QtCore import QByteArray, QDate, QObject, QProcess, Signal
from PySide6.QtWidgets import QApplication, QLineEdit
from PySide6.QtTest import QTest

from desktop.agent_panel import AgentPanel, ModelSettingsDialog
from desktop.main import MainWindow
from desktop.services import Store
from research_agent.config import AgentConfig, load_agent_config, save_agent_config


class FakeProcess(QObject):
    readyReadStandardOutput = Signal()
    readyReadStandardError = Signal()
    errorOccurred = Signal(object)
    finished = Signal(int, object)
    ProcessError = QProcess.ProcessError
    ExitStatus = QProcess.ExitStatus

    def __init__(self, parent=None):
        super().__init__(parent)
        self.output = self.errors = self.request = b""
        self.started = None
        self.killed = False

    def setProcessEnvironment(self, env):
        self.environment = env

    def setWorkingDirectory(self, directory):
        self.directory = directory

    def start(self, program, arguments):
        self.started = (program, arguments)

    def write(self, value):
        self.request += bytes(value)
        return len(value)

    def closeWriteChannel(self):
        self.input_closed = True

    def readAllStandardOutput(self):
        result, self.output = self.output, b""
        return QByteArray(result)

    def readAllStandardError(self):
        result, self.errors = self.errors, b""
        return QByteArray(result)

    def feed(self, value):
        self.output += value
        self.readyReadStandardOutput.emit()

    def kill(self):
        self.killed = True

    def waitForFinished(self, _timeout):
        return True


class AgentUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent_ui_")
        self.root = Path(self.temp.name)
        stocks = self.root / "data/full_a/stocks"
        stocks.mkdir(parents=True)
        dates = pd.bdate_range(end="2026-09-30", periods=70)
        pd.DataFrame({"date": dates, "code": "000001", "name": "合成股票",
                      "open": 10, "high": 11, "low": 9, "close": 10, "volume": 1000}).to_csv(
            stocks / "000001.csv", index=False)
        pd.DataFrame([{"code": "000001", "status": "updated", "new_last_date": "2026-09-30"}]).to_csv(
            self.root / "data/full_a/daily_update_status.csv", index=False)
        python = self.root / ".venv/Scripts/python.exe"
        python.parent.mkdir(parents=True)
        python.touch()
        self.env = mock.patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.fake = mock.patch("desktop.agent_panel.QProcess", FakeProcess)
        self.fake.start()
        self.store = Store(self.root)
        self.panels = []
        self.windows = []

    def tearDown(self):
        for window in self.windows:
            window.task = None
            window.agent_panel.running = False
            window.close()
            window.deleteLater()
        for panel in self.panels:
            panel.shutdown()
            panel.close()
            panel.deleteLater()
        self.app.processEvents()
        self.fake.stop()
        self.env.stop()
        self.temp.cleanup()

    def configure(self):
        save_agent_config(self.root, AgentConfig(base_url="http://127.0.0.1:9/v1",
                                               model="synthetic-model", api_key="ui-synthetic-key"))

    def panel(self, configured=True):
        if configured:
            self.configure()
        panel = AgentPanel(self.store)
        self.panels.append(panel)
        return panel

    def start(self, panel):
        panel.input.setPlainText("这只股票的走势怎样？")
        panel.submit()
        self.assertTrue(panel.running)
        return panel.process

    def result(self, panel, status="completed", newline=True):
        result = {"status": status, "answer": "合成结论 <script> 不作为页面执行",
                  "as_of": "2026-09-30", "model": "synthetic-model",
                  "sources": [{"path": "data/full_a/stocks/000001.csv", "date": "2026-09-30"}],
                  "warnings": ["离线合成数据"],
                  "tool_calls": [{"name": "get_bars", "status": "ok", "data_date": "2026-09-30"}],
                  "history": [{"role": "user", "content": "走势？"},
                              {"role": "assistant", "content": "合成结论"}]}
        value = json.dumps({"type": "agent_result", "result": result}, ensure_ascii=False)
        data = (value + ("\n" if newline else "")).encode("utf-8")
        # Split inside a Chinese UTF-8 sequence, as a real pipe can do.
        split = data.index("合".encode("utf-8")) + 1
        panel.process.feed(data[:split])
        panel.process.feed(data[split:])
        return result

    def test_missing_configuration_never_starts_or_creates_config(self):
        panel = self.panel(False)
        self.assertFalse(panel.send_button.isEnabled())
        panel.input.setPlainText("查询数据")
        panel.submit()
        self.assertIsNone(panel.process)
        self.assertFalse((self.root / ".env.local").exists())

    def test_settings_masks_retains_and_clears_existing_key(self):
        self.configure()
        dialog = ModelSettingsDialog(self.root)
        self.assertEqual(dialog.api_key.echoMode(), QLineEdit.EchoMode.Password)
        self.assertEqual(dialog.api_key.text(), "")
        dialog.timeout.setValue(1.5)
        dialog.save()
        config = load_agent_config(self.root)
        self.assertEqual(config.api_key, "ui-synthetic-key")
        self.assertEqual(config.timeout_seconds, 1.5)
        dialog = ModelSettingsDialog(self.root)
        dialog.clear_key.setChecked(True)
        dialog.save()
        self.assertEqual(load_agent_config(self.root).api_key, "")

    def test_local_mode_presets_save_without_key_or_network(self):
        dialog = ModelSettingsDialog(self.root)
        self.assertEqual(dialog.mode.currentData(), "cloud")
        dialog.mode.setCurrentIndex(dialog.mode.findData("local"))
        self.assertEqual(dialog.local_runtime.currentData(), "ollama")
        self.assertEqual(dialog.base_url.text(), "http://127.0.0.1:11434/v1")
        dialog.local_runtime.setCurrentIndex(dialog.local_runtime.findData("lm_studio"))
        self.assertEqual(dialog.base_url.text(), "http://127.0.0.1:1234/v1")
        dialog.model.setText("synthetic-local-tools")
        with mock.patch("desktop.agent_panel.discover_local_models") as discover:
            dialog.save()
        discover.assert_not_called()
        config = load_agent_config(self.root)
        self.assertEqual(config.connection_mode, "local")
        self.assertEqual(config.local_runtime, "lm_studio")
        self.assertEqual(config.api_key, "")
        self.assertTrue(config.ready)
        self.assertIn("是否离线", dialog.notice.text())

    def test_switch_from_cloud_does_not_reuse_cloud_credentials(self):
        save_agent_config(self.root, AgentConfig(base_url="https://synthetic.example/v1",
                                               model="cloud-model", api_key="cloud-synthetic-key"))
        dialog = ModelSettingsDialog(self.root)
        dialog.mode.setCurrentIndex(dialog.mode.findData("local"))
        dialog.model.setText("local-model")
        dialog.save()
        config = load_agent_config(self.root)
        self.assertEqual(config.api_key, "")
        self.assertEqual(config.effective_mode, "local")
        self.assertNotIn("cloud-synthetic-key", (self.root / ".env.local").read_text())

    def test_mode_switch_retains_separate_unsaved_drafts(self):
        dialog = ModelSettingsDialog(self.root)
        dialog.base_url.setText("https://synthetic.example/v1")
        dialog.model.setText("cloud-model")
        dialog.api_key.setText("synthetic-cloud-key")
        dialog.mode.setCurrentIndex(dialog.mode.findData("local"))
        self.assertEqual(dialog.api_key.text(), "")
        dialog.model.setText("local-model")
        dialog.mode.setCurrentIndex(dialog.mode.findData("cloud"))
        self.assertEqual(dialog.model.text(), "cloud-model")
        self.assertEqual(dialog.api_key.text(), "synthetic-cloud-key")
        dialog.mode.setCurrentIndex(dialog.mode.findData("local"))
        self.assertEqual(dialog.model.text(), "local-model")
        self.assertEqual(dialog.api_key.text(), "")

    def test_changed_service_clears_retained_key(self):
        self.configure()
        dialog = ModelSettingsDialog(self.root)
        dialog.base_url.setText("http://127.0.0.1:1234/v1")
        dialog.save()
        self.assertEqual(load_agent_config(self.root).api_key, "")

    def test_changed_service_also_clears_typed_key(self):
        dialog = ModelSettingsDialog(self.root)
        dialog.base_url.setText("https://synthetic-a.example/v1")
        dialog.api_key.setText("synthetic-key-for-a")
        dialog.base_url.setText("https://synthetic-b.example/v1")
        self.assertEqual(dialog.api_key.text(), "")

    def test_credential_edit_cancels_pending_discovery(self):
        for edit in ("key", "clear"):
            with self.subTest(edit=edit):
                dialog = ModelSettingsDialog(self.root)
                dialog.mode.setCurrentIndex(dialog.mode.findData("local"))
                started, release = threading.Event(), threading.Event()
                cancellations = []

                def delayed(*args, **kwargs):
                    cancellations.append(kwargs["cancel"])
                    started.set()
                    release.wait(2)
                    return ["late-model"]

                with mock.patch("desktop.agent_panel.discover_local_models", side_effect=delayed):
                    dialog.discover_models()
                    self.assertTrue(started.wait(1))
                    if edit == "key":
                        dialog.api_key.setText("new-synthetic-key")
                    else:
                        dialog.clear_key.setChecked(True)
                    self.assertTrue(cancellations[0].is_set())
                    release.set()
                    QTest.qWait(100)
                self.assertEqual(dialog.model.text(), "")
                self.assertFalse(dialog._probe_timer.isActive())
                dialog.close()

    def test_local_remote_address_rejected_without_config_write(self):
        dialog = ModelSettingsDialog(self.root)
        dialog.mode.setCurrentIndex(dialog.mode.findData("local"))
        dialog.base_url.setText("https://synthetic.example/v1")
        dialog.model.setText("synthetic-model")
        dialog.save()
        self.assertTrue(dialog.error.text())
        self.assertFalse((self.root / ".env.local").exists())

    def wait_probe(self, dialog):
        deadline = time.monotonic() + 2
        while not dialog.discover_button.isEnabled() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(dialog.discover_button.isEnabled())

    def test_model_discovery_async_selects_model_and_closes_cleanly(self):
        dialog = ModelSettingsDialog(self.root)
        dialog.mode.setCurrentIndex(dialog.mode.findData("local"))
        with mock.patch("desktop.agent_panel.discover_local_models", return_value=["local-a", "local-b"]) as discover:
            dialog.discover_models()
            self.wait_probe(dialog)
        self.assertEqual(dialog.model.text(), "local-a")
        dialog.models.setCurrentIndex(2)
        self.assertEqual(dialog.model.text(), "local-b")
        self.assertEqual(discover.call_args.kwargs["api_key"], "")
        self.assertIn("2 个模型", dialog.connection_status.text())
        dialog.reject()
        self.assertFalse(dialog._probe_timer.isActive())

    def test_discovery_cancel_drops_late_list_and_cloud_key(self):
        save_agent_config(self.root, AgentConfig(base_url="https://synthetic.example/v1",
                                               model="cloud-model", api_key="cloud-synthetic-key"))
        dialog = ModelSettingsDialog(self.root)
        dialog.mode.setCurrentIndex(dialog.mode.findData("local"))
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        keys = []

        def delayed(*args, **kwargs):
            keys.append(kwargs["api_key"])
            started.set()
            release.wait(2)
            finished.set()
            return ["late-model"]

        with mock.patch("desktop.agent_panel.discover_local_models", side_effect=delayed):
            dialog.discover_models()
            self.assertTrue(started.wait(1))
            dialog.base_url.setText("http://127.0.0.1:1234/v1")
            release.set()
            self.assertTrue(finished.wait(1))
            QTest.qWait(100)
        self.assertEqual(keys, [""])
        self.assertNotIn("late-model", dialog.model.text())
        self.assertFalse(dialog._probe_timer.isActive())
        self.assertTrue(dialog.discover_button.isEnabled())
        dialog.close()

    def test_discovery_empty_or_failure_allows_manual_model(self):
        dialog = ModelSettingsDialog(self.root)
        dialog.mode.setCurrentIndex(dialog.mode.findData("local"))
        with mock.patch("desktop.agent_panel.discover_local_models", return_value=[]):
            dialog.discover_models()
            self.wait_probe(dialog)
        self.assertIn("没有可用模型", dialog.connection_status.text())
        dialog.model.setText("manual-model")
        with mock.patch("desktop.agent_panel.discover_local_models", side_effect=RuntimeError("synthetic-secret")):
            dialog.discover_models()
            self.wait_probe(dialog)
        self.assertNotIn("synthetic-secret", dialog.connection_status.text())
        self.assertEqual(dialog.model.text(), "manual-model")
        dialog.close()

    def test_context_stdin_trace_utf8_and_final_without_newline(self):
        panel = self.panel()
        panel.set_current_stock("000001", "合成股票")
        self.assertEqual(panel.date_edit.date(), QDate(2026, 9, 30))
        process = self.start(panel)
        request = json.loads(process.request)
        self.assertIn("000001", request["prompt"])
        self.assertEqual(request["as_of"], "2026-09-30")
        self.assertTrue(process.input_closed)
        self.assertNotIn("ui-synthetic-key", process.request.decode())
        self.assertNotIn("ui-synthetic-key", repr(process.started))
        process.feed((json.dumps({"type": "agent_progress", "message": "查询 get_bars"}) + "\n").encode())
        self.assertIn("get_bars", panel.activity.toPlainText())
        self.result(panel, newline=False)
        process.finished.emit(0, QProcess.ExitStatus.NormalExit)
        self.assertFalse(panel.running)
        self.assertEqual(len(panel.history), 2)
        self.assertEqual(panel.input.toPlainText(), "")
        self.assertIn("<script>", panel.chat.toPlainText())
        self.assertIn("000001.csv", panel.chat.toPlainText())
        self.assertIn("get_bars · ok", panel.activity.toPlainText())

    def test_incomplete_preserves_question_and_history(self):
        panel = self.panel()
        process = self.start(panel)
        self.result(panel, status="incomplete")
        process.finished.emit(0, QProcess.ExitStatus.NormalExit)
        self.assertIn("走势", panel.input.toPlainText())
        self.assertEqual(panel.history, [])
        self.assertIn("未完成", panel.status_label.text())

    def test_cancel_ignores_late_result_and_hides_stderr(self):
        panel = self.panel()
        process = self.start(panel)
        process.errors = b"unexpected ui-synthetic-key stack trace"
        process.readyReadStandardError.emit()
        panel.cancel()
        self.result(panel)
        process.finished.emit(1, QProcess.ExitStatus.CrashExit)
        self.assertTrue(process.killed)
        self.assertFalse(panel.received_result)
        self.assertEqual(panel.history, [])
        self.assertNotIn("ui-synthetic-key", panel.activity.toPlainText() + panel.chat.toPlainText())
        self.assertIn("已停止", panel.status_label.text())

    def test_date_change_discards_future_chat_facts(self):
        panel = self.panel()
        panel.history = [{"role": "assistant", "content": "9 月 30 日的价格"}]
        panel.history_as_of = "2026-09-30"
        panel.latest_date.setChecked(False)
        panel.date_edit.setDate(QDate(2026, 9, 20))
        process = self.start(panel)
        request = json.loads(process.request)
        self.assertEqual(request["as_of"], "2026-09-20")
        self.assertEqual(request["history"], [])

    def test_duplicate_results_ignored(self):
        panel = self.panel()
        process = self.start(panel)
        result = self.result(panel)
        count = len(panel.messages)
        panel.handle_line(json.dumps({"type": "agent_result", "result": result}))
        self.assertEqual(len(panel.messages), count)
        process.finished.emit(0, QProcess.ExitStatus.NormalExit)

    def test_invalid_result_status_and_history_do_not_crash(self):
        panel = self.panel()
        process = self.start(panel)
        panel.handle_line(json.dumps({"type": "agent_result", "result": {"status": [], "answer": "test"}}))
        self.assertFalse(panel.received_result)
        panel.handle_line(json.dumps({"type": "agent_result", "result": {
            "status": "completed", "answer": "test", "as_of": "2026-09-30",
            "history": [{"role": {}, "content": "invalid"}]}}))
        process.finished.emit(0, QProcess.ExitStatus.NormalExit)
        self.assertEqual(panel.history, [])

    def test_missing_market_status_passes_none_for_safe_fallback(self):
        panel = self.panel()
        (self.root / "data/full_a/daily_update_status.csv").unlink()
        process = self.start(panel)
        self.assertIsNone(json.loads(process.request)["as_of"])

    def test_start_failure_preserves_question(self):
        panel = self.panel()
        process = self.start(panel)
        process.errorOccurred.emit(QProcess.ProcessError.FailedToStart)
        self.assertFalse(panel.running)
        self.assertIsNone(panel.process)
        self.assertIn("走势", panel.input.toPlainText())

    def test_market_and_research_tasks_are_mutually_exclusive(self):
        self.configure()
        window = MainWindow(self.store)
        self.windows.append(window)
        panel = window.agent_panel
        process = self.start(panel)
        self.assertFalse(window.update_button.isEnabled())
        window.start_task("update")
        self.assertIsNone(window.task)
        window.add_watch("000001")
        self.assertEqual(self.store.watchlist(), [])
        window.search.setText("000001")
        self.assertFalse(window.add_button.isEnabled())
        panel.cancel()
        process.finished.emit(1, QProcess.ExitStatus.CrashExit)
        self.assertTrue(window.update_button.isEnabled())
        window.task = "update"
        window.set_busy(True)
        self.assertFalse(panel.send_button.isEnabled())
        panel.submit()
        self.assertIsNone(panel.process)
        window.task = None
        window.set_busy(False)
        self.assertTrue(panel.send_button.isEnabled())


if __name__ == "__main__":
    unittest.main()
