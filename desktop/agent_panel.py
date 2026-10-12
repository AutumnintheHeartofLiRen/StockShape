"""Desktop research conversation and local model settings. No network on UI thread."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import html
import json
from pathlib import Path
import queue
import threading

from PySide6.QtCore import QDate, QProcess, QProcessEnvironment, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QPlainTextEdit, QPushButton,
    QTextBrowser, QVBoxLayout, QWidget,
)

from research_agent.config import AgentConfig, load_agent_config, save_agent_config
from research_agent.discovery import DiscoveryError, discover_local_models
from research_agent.provider import AgentCancelled


LOCAL_PRESETS = {
    "ollama": ("Ollama", "http://127.0.0.1:11434/v1"),
    "lm_studio": ("LM Studio", "http://127.0.0.1:1234/v1"),
    "custom": ("自定义本地服务", ""),
}


class ModelSettingsDialog(QDialog):
    def __init__(self, root, parent=None):
        super().__init__(parent)
        self.root = Path(root)
        try:
            self.config = load_agent_config(self.root)
        except (ValueError, OSError):
            self.config = AgentConfig()
        self._mode = self.config.effective_mode
        self._drafts = {}
        self._probe_cancel = None
        self._probe_queue = None
        self._probe_timer = QTimer(self)
        self._probe_timer.setInterval(80)
        self._probe_timer.timeout.connect(self.poll_models)
        self.setWindowTitle("AI 模型设置")
        self.setMinimumWidth(600)
        layout = QVBoxLayout(self)
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        self.form = QFormLayout()
        self.mode = QComboBox()
        self.mode.addItem("云端 API", "cloud")
        self.mode.addItem("本地模型", "local")
        self.mode.setCurrentIndex(self.mode.findData(self._mode))
        self.local_runtime = QComboBox()
        for runtime, (name, _) in LOCAL_PRESETS.items():
            self.local_runtime.addItem(name, runtime)
        runtime = self.config.local_runtime
        if runtime == "custom":
            runtime = next((name for name, (_, url) in LOCAL_PRESETS.items()
                            if url and url == self.config.base_url.rstrip("/")), "custom")
        self.local_runtime.setCurrentIndex(self.local_runtime.findData(runtime))
        self.base_url = QLineEdit(self.config.base_url)
        self.model = QLineEdit(self.config.model)
        self.models = QComboBox()
        self.models.addItem("先检测本地服务，或直接填写模型名称", "")
        self.models.setEnabled(False)
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.clear_key = QCheckBox("清除已保存的密钥")
        self.timeout = QDoubleSpinBox()
        self.timeout.setRange(1, 180)
        self.timeout.setDecimals(1)
        self.timeout.setValue(self.config.timeout_seconds)
        self.timeout.setSuffix(" 秒")
        self.discover_button = QPushButton("检测连接并读取模型")
        self.discover_button.clicked.connect(self.discover_models)
        self.form.addRow("运行方式", self.mode)
        self.form.addRow("本地服务", self.local_runtime)
        self.form.addRow("服务地址", self.base_url)
        self.form.addRow("模型名称", self.model)
        self.form.addRow("可用模型", self.models)
        self.form.addRow("", self.discover_button)
        self.form.addRow("API key", self.api_key)
        self.form.addRow("", self.clear_key)
        self.form.addRow("请求超时", self.timeout)
        layout.addLayout(self.form)
        self.connection_status = QLabel()
        self.connection_status.setWordWrap(True)
        self.connection_status.setStyleSheet("color:#42639a;")
        layout.addWidget(self.connection_status)
        self.notice = QLabel()
        self.notice.setWordWrap(True)
        self.notice.setStyleSheet("color:#78869c; padding:8px 0;")
        layout.addWidget(self.notice)
        self.error = QLabel("")
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color:#c04450;")
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.mode.currentIndexChanged.connect(self.change_mode)
        self.local_runtime.currentIndexChanged.connect(self.change_runtime)
        self.base_url.textChanged.connect(self.address_changed)
        self.api_key.textChanged.connect(self.credentials_changed)
        self.clear_key.toggled.connect(self.credentials_changed)
        self.models.currentIndexChanged.connect(self.select_model)
        self.update_mode()

    def update_mode(self):
        local = self._mode == "local"
        for field in (self.local_runtime, self.models, self.discover_button):
            self.form.setRowVisible(field, local)
        self.connection_status.setVisible(local)
        self.base_url.setPlaceholderText("http://127.0.0.1:端口/v1" if local else "https://服务商地址/v1")
        self.model.setPlaceholderText("选择或填写本机已安装、支持工具调用的模型" if local else "填写服务商提供的模型名称")
        self.form.labelForField(self.api_key).setText("API key（可选）" if local else "API key")
        self.hint.setText("连接本机已启动的 Ollama、LM Studio 或兼容服务。请先在对应软件中下载/加载支持工具调用的模型。"
                          if local else "连接支持工具调用的云端模型服务。配置仅保存在本机 .env.local。")
        target = "请求发送给本机服务；是否离线取决于所选模型，本地服务也可能转发至云端。" if local else "云端服务会收到问题、对话上下文和查询摘要。"
        self.notice.setText(target + "系统不会自动发送私有策略源码、参数文件或整份行情库。更换服务地址后需重新填写所需密钥；非空 AI_* 环境变量优先于本地文件。")
        self.refresh_key_hint()

    def refresh_key_hint(self):
        retained = (self._mode == self.config.effective_mode and
                    self.base_url.text().strip().rstrip("/") == self.config.base_url.rstrip("/"))
        self.api_key.setPlaceholderText("已配置，留空保留" if retained and self.config.api_key else
                                       "本地服务通常可留空" if self._mode == "local" else "填写该服务的 API key")

    def change_mode(self, *_):
        self.cancel_discovery()
        self._drafts[self._mode] = (self.base_url.text(), self.model.text(), self.api_key.text(),
                                    self.local_runtime.currentData(), self.clear_key.isChecked())
        self._mode = self.mode.currentData()
        default = (LOCAL_PRESETS["ollama"][1], "", "", "ollama", False) if self._mode == "local" else ("", "", "", "custom", False)
        url, model, key, runtime, clear = self._drafts.get(self._mode, default)
        self.local_runtime.blockSignals(True)
        self.local_runtime.setCurrentIndex(self.local_runtime.findData(runtime))
        self.local_runtime.blockSignals(False)
        self.base_url.setText(url)
        self.model.setText(model)
        self.api_key.setText(key)
        self.clear_key.setChecked(clear)
        self.error.clear()
        self.update_mode()

    def change_runtime(self, *_):
        self.cancel_discovery()
        preset = LOCAL_PRESETS[self.local_runtime.currentData()][1]
        if preset and self.base_url.text().rstrip("/") != preset:
            self.base_url.setText(preset)
            self.model.clear()
            self.api_key.clear()

    def address_changed(self, *_):
        self.api_key.blockSignals(True)
        self.api_key.clear()
        self.api_key.blockSignals(False)
        self.credentials_changed()
        self.refresh_key_hint()

    def credentials_changed(self, *_):
        self.cancel_discovery()
        self.models.blockSignals(True)
        self.models.clear()
        self.models.addItem("先检测本地服务，或直接填写模型名称", "")
        self.models.blockSignals(False)
        self.models.setEnabled(False)
        self.connection_status.clear()

    def select_model(self, *_):
        value = self.models.currentData()
        if value:
            self.model.setText(value)

    def cancel_discovery(self):
        if self._probe_cancel:
            self._probe_cancel.set()
        self._probe_cancel = self._probe_queue = None
        self._probe_timer.stop()
        if hasattr(self, "discover_button"):
            self.discover_button.setEnabled(True)

    def discover_models(self):
        if self._mode != "local":
            return
        self.cancel_discovery()
        self._probe_cancel = cancel = threading.Event()
        self._probe_queue = output = queue.Queue(maxsize=1)
        url = self.base_url.text().strip()
        key = self.api_key.text().strip()
        if not key and not self.clear_key.isChecked() and url.rstrip("/") == self.config.base_url.rstrip("/") and self.config.effective_mode == "local":
            key = self.config.api_key
        self.discover_button.setEnabled(False)
        self.connection_status.setText("正在连接本机服务并读取模型列表…")

        def discover():
            try:
                result = (True, discover_local_models(url, api_key=key, timeout_seconds=5, cancel=cancel))
            except AgentCancelled:
                return
            except DiscoveryError as exc:
                result = (False, str(exc))
            except Exception:
                result = (False, "本地模型列表读取失败，请检查服务是否已启动。")
            if not cancel.is_set():
                output.put(result)

        threading.Thread(target=discover, name="stockshape-local-models", daemon=True).start()
        self._probe_timer.start()

    def poll_models(self):
        if self._probe_queue is None:
            return
        try:
            success, result = self._probe_queue.get_nowait()
        except queue.Empty:
            return
        self.cancel_discovery()
        if not success:
            self.connection_status.setText(result)
            return
        self.models.blockSignals(True)
        self.models.clear()
        self.models.addItem("选择一个模型", "")
        for name in result:
            self.models.addItem(name, name)
        self.models.setEnabled(bool(result))
        self.models.blockSignals(False)
        if result and not self.model.text().strip():
            self.models.setCurrentIndex(1)
        elif self.model.text().strip() in result:
            self.models.setCurrentIndex(self.models.findData(self.model.text().strip()))
        self.connection_status.setText(f"服务已连接，读取到 {len(result)} 个模型。研究还需要模型支持工具调用。"
                                       if result else "服务已连接，但没有可用模型。请先在本地服务中下载或加载模型。")

    def done(self, result):
        self.cancel_discovery()
        super().done(result)

    def closeEvent(self, event):
        self.cancel_discovery()
        super().closeEvent(event)

    def save(self):
        same_service = (self._mode == self.config.effective_mode and
                        self.base_url.text().strip().rstrip("/") == self.config.base_url.rstrip("/"))
        key = "" if self.clear_key.isChecked() else (self.api_key.text().strip() or (self.config.api_key if same_service else ""))
        config = replace(self.config, provider="openai_compatible", base_url=self.base_url.text().strip(),
                         model=self.model.text().strip(), api_key=key, timeout_seconds=self.timeout.value(),
                         connection_mode=self._mode, local_runtime=self.local_runtime.currentData() if self._mode == "local" else "custom")
        try:
            self.config = save_agent_config(self.root, config)
        except (ValueError, OSError) as exc:
            self.error.setText(str(exc) if isinstance(exc, ValueError) else "配置保存失败，请检查文件是否被占用。")
            return
        self.accept()


class AgentPanel(QWidget):
    busyChanged = Signal(bool)

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self.root = Path(store.root)
        self.running = False
        self.external_busy = False
        self.process = None
        self.stdout_buffer = b""
        self.cancelled = False
        self.received_result = False
        self.task_error = ""
        self.history = []
        self.history_as_of = None
        self.messages = []
        self.current_code = None
        self.current_name = ""
        self._build_ui()
        self.set_market_date()
        self.refresh_config()
        self._render_chat()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(12)
        header = QHBoxLayout()
        title = QLabel("AI 研究助手")
        title.setStyleSheet("font-size:21px;font-weight:600;color:#172743;")
        header.addWidget(title)
        header.addStretch()
        self.model_badge = QLabel()
        self.model_badge.setStyleSheet("color:#42639a;")
        header.addWidget(self.model_badge)
        self.settings_button = QPushButton("模型设置")
        self.settings_button.clicked.connect(self.open_settings)
        header.addWidget(self.settings_button)
        self.clear_button = QPushButton("新对话")
        self.clear_button.clicked.connect(self.clear_chat)
        header.addWidget(self.clear_button)
        layout.addLayout(header)
        self.context_label = QLabel("先选择股票，或直接提问")
        self.context_label.setWordWrap(True)
        self.context_label.setStyleSheet("color:#78869c;")
        layout.addWidget(self.context_label)
        quick = QHBoxLayout()
        self.stock_question = QPushButton("当前股票摘要")
        self.stock_question.clicked.connect(self.ask_current_stock)
        self.watch_question = QPushButton("自选股概览")
        self.watch_question.clicked.connect(lambda: self.prepare_question("请查看我的自选股，结合本地最新行情和已有分析概括观察重点，并说明缺失的数据。"))
        self.market_question = QPushButton("本地数据状态")
        self.market_question.clicked.connect(lambda: self.prepare_question("本地行情更新到哪一天了？覆盖情况怎么样，还有哪些数据限制？"))
        for button in (self.stock_question, self.watch_question, self.market_question):
            quick.addWidget(button)
        quick.addStretch()
        layout.addLayout(quick)
        self.chat = QTextBrowser()
        self.chat.setObjectName("agentChat")
        self.chat.setOpenExternalLinks(False)
        self.chat.setOpenLinks(False)
        self.chat.setStyleSheet("QTextBrowser{background:#f7f9fc;border:1px solid #e2e7ef;border-radius:9px;padding:10px;}")
        layout.addWidget(self.chat, 1)
        self.activity = QPlainTextEdit()
        self.activity.setObjectName("agentActivity")
        self.activity.setReadOnly(True)
        self.activity.setMaximumBlockCount(150)
        self.activity.setFixedHeight(82)
        self.activity.setPlaceholderText("工具查询过程和来源会显示在这里")
        layout.addWidget(self.activity)
        date_row = QHBoxLayout()
        self.latest_date = QCheckBox("以本地最新交易日为截止日")
        self.latest_date.setChecked(True)
        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        self.date_edit.setDate(QDate.fromString(today, "yyyy-MM-dd"))
        self.date_edit.setEnabled(False)
        self.latest_date.toggled.connect(self.cutoff_mode_changed)
        date_row.addWidget(self.latest_date)
        date_row.addWidget(self.date_edit)
        date_row.addStretch()
        layout.addLayout(date_row)
        self.input = QPlainTextEdit()
        self.input.setObjectName("agentPrompt")
        self.input.setPlaceholderText("例如：这只股票最近走势怎样？已有分析是否与当前行情日期一致？")
        self.input.setFixedHeight(88)
        layout.addWidget(self.input)
        bottom = QHBoxLayout()
        self.status_label = QLabel("先在模型设置中选择云端服务或本地模型")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color:#78869c;")
        bottom.addWidget(self.status_label, 1)
        self.stop_button = QPushButton("停止")
        self.stop_button.clicked.connect(self.cancel)
        self.stop_button.hide()
        bottom.addWidget(self.stop_button)
        self.send_button = QPushButton("开始研究")
        self.send_button.setObjectName("primary")
        self.send_button.clicked.connect(self.submit)
        bottom.addWidget(self.send_button)
        layout.addLayout(bottom)
        self.privacy_notice = QLabel()
        self.privacy_notice.setWordWrap(True)
        self.privacy_notice.setStyleSheet("font-size:11px;color:#8792a2;")
        layout.addWidget(self.privacy_notice)

    def refresh_config(self):
        try:
            self.config = load_agent_config(self.root)
        except (OSError, ValueError):
            self.config = AgentConfig()
            self.status_label.setText("模型配置读取失败，请在模型设置中修正。")
        mode = "本地" if self.config.effective_mode == "local" else "云端"
        self.model_badge.setText(f"{mode} · {self.config.model}" if self.config.ready else "模型未配置")
        target = "请求发送至本机服务，是否离线由所选模型决定" if self.config.effective_mode == "local" else "云端模型接收问题与查询摘要"
        self.privacy_notice.setText("只读查询 · 对话仅保留在本次窗口内 · " + target + " · 私有参数和源码不发送")
        self._sync_controls()

    def set_market_date(self, value=None):
        if not self.latest_date.isChecked():
            return
        if not value:
            try:
                value = self.store.market_status().get("date")
            except Exception:
                return
        day = QDate.fromString(str(value or ""), "yyyy-MM-dd")
        if day.isValid():
            self.date_edit.setDate(day)

    def cutoff_mode_changed(self, checked):
        self.date_edit.setEnabled(not checked and not self.running)
        if checked:
            self.set_market_date()

    def open_settings(self):
        if self.running:
            return
        dialog = ModelSettingsDialog(self.root, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.history.clear()
            self.history_as_of = None
            self.refresh_config()
            self.status_label.setText("配置已保存，可以开始研究" if self.config.ready else "配置已保存，请补齐服务地址、模型名和所需密钥")

    def set_current_stock(self, code, name=""):
        self.current_code, self.current_name = code, name
        self.context_label.setText(f"当前股票：{code} {name} · 发送问题时固定本次研究范围")
        self._sync_controls()

    def ask_current_stock(self):
        if self.current_code:
            self.prepare_question(f"请根据本地数据概括 {self.current_code} {self.current_name} 的近期走势，查看已有选股分析，说明数据截止日期与需要注意的限制。")

    def prepare_question(self, text):
        if not self.running:
            self.input.setPlainText(text)
            self.input.setFocus()

    def clear_chat(self):
        if self.running:
            return
        self.messages.clear()
        self.history.clear()
        self.history_as_of = None
        self.activity.clear()
        self._render_chat()
        self.status_label.setText("已开始新对话")

    def set_external_busy(self, busy):
        self.external_busy = busy
        self._sync_controls()

    def _sync_controls(self):
        free = not self.running and not self.external_busy
        self.send_button.setEnabled(free and self.config.ready)
        self.settings_button.setEnabled(not self.running)
        self.clear_button.setEnabled(not self.running)
        self.input.setReadOnly(self.running)
        for button in (self.stock_question, self.watch_question, self.market_question):
            button.setEnabled(free and (button is not self.stock_question or bool(self.current_code)))
        self.latest_date.setEnabled(not self.running)
        self.date_edit.setEnabled(not self.running and not self.latest_date.isChecked())
        self.stop_button.setVisible(self.running)

    def _render_chat(self):
        if not self.messages:
            self.chat.setHtml('<div style="padding:26px;color:#78869c;"><h3 style="color:#334155;">从本地事实开始研究</h3><p>你可以查询股票走势、自选概览、已有分析和行情状态。</p><p>先选择云端服务或本地模型，再输入问题。模型须支持工具调用。</p></div>')
            return
        parts = []
        for role, text in self.messages:
            title = {"user": "你", "assistant": "研究助手", "error": "任务提示"}.get(role, role)
            color = "#306bdc" if role == "user" else "#c04450" if role == "error" else "#172743"
            safe = html.escape(str(text)).replace("\n", "<br>")
            parts.append(f'<div style="margin:12px 0;"><b style="color:{color};">{title}</b><p style="line-height:1.55;">{safe}</p></div>')
        self.chat.setHtml("".join(parts))
        self.chat.verticalScrollBar().setValue(self.chat.verticalScrollBar().maximum())

    def submit(self):
        if self.running or self.external_busy:
            return
        self.refresh_config()
        if not self.config.ready:
            self.status_label.setText("请先在模型设置中选择运行方式，补齐服务地址、模型名和所需密钥")
            return
        prompt = self.input.toPlainText().strip()
        if not prompt:
            self.status_label.setText("请输入研究问题")
            return
        if len(prompt) > 6000:
            self.status_label.setText("问题过长，请缩短到 6000 字符以内")
            return
        python = self.root / ".venv/Scripts/python.exe"
        if not python.is_file():
            self.status_label.setText("未找到项目 Python 环境，请按维护说明恢复 .venv")
            return
        task_date = self.date_edit.date().toString("yyyy-MM-dd")
        if self.latest_date.isChecked():
            try:
                task_date = self.store.market_status().get("date") or None
            except Exception:
                task_date = None
            self.set_market_date(task_date)
        if self.history_as_of != task_date:
            self.history.clear()
        context = f"当前查看股票：{self.current_code} {self.current_name[:64]}。\n" if self.current_code else ""
        task_prompt = context + prompt
        if len(task_prompt) > 6000:
            self.status_label.setText("问题过长，请缩短后再试")
            return
        request = {"prompt": task_prompt, "history": self.history, "as_of": task_date}
        self.running = True
        self.cancelled = self.received_result = False
        self.task_error = ""
        self.stdout_buffer = b""
        self.messages.append(("user", prompt))
        self._render_chat()
        self.activity.clear()
        self.status_label.setText("正在查询本地事实并准备研究…")
        self._sync_controls()
        self.busyChanged.emit(True)
        self.process = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUTF8", "1")
        env.insert("PYTHONUNBUFFERED", "1")
        env.insert("PYTHONDONTWRITEBYTECODE", "1")
        env.insert("STOCKSHAPE_ROOT", str(self.root))
        self.process.setProcessEnvironment(env)
        self.process.setWorkingDirectory(str(self.root))
        self.process.readyReadStandardOutput.connect(self.read_stdout)
        self.process.readyReadStandardError.connect(self.read_stderr)
        self.process.errorOccurred.connect(self.process_error)
        self.process.finished.connect(self.finished)
        self.process.start(str(python), ["-X", "utf8", "-u", "-m", "desktop.agent_worker"])
        self.process.write((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
        self.process.closeWriteChannel()

    def read_stdout(self):
        if self.process is None:
            return
        self.stdout_buffer += bytes(self.process.readAllStandardOutput())
        while b"\n" in self.stdout_buffer:
            line, self.stdout_buffer = self.stdout_buffer.split(b"\n", 1)
            self.handle_line(line.decode("utf-8", errors="replace"))

    def read_stderr(self):
        if self.process and bytes(self.process.readAllStandardError()):
            # Unexpected child traces can include request contents; don't echo.
            self.activity.appendPlainText("后台产生诊断信息，已隐藏原始内容。")

    def handle_line(self, line):
        if self.cancelled:
            return
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            return
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "agent_progress":
            message = str(event.get("message", "处理中"))[:500]
            self.status_label.setText(message)
            self.activity.appendPlainText(message)
        elif kind == "agent_error":
            self.task_error = str(event.get("message", "AI 任务失败"))
        elif kind == "agent_result" and not self.received_result:
            result = event.get("result")
            if (not isinstance(result, dict) or not isinstance(result.get("answer"), str)
                    or not isinstance(result.get("status"), str)
                    or result["status"] not in {"completed", "incomplete"}):
                self.task_error = "AI 返回结果格式无效"
                return
            self.received_result = True
            self.messages.append(("assistant", result["answer"]))
            status = result["status"]
            new_history = result.get("history", [])
            valid_history = (isinstance(new_history, list) and all(
                isinstance(item, dict) and set(item) == {"role", "content"}
                and isinstance(item["role"], str) and item["role"] in {"user", "assistant"}
                and isinstance(item["content"], str)
                for item in new_history))
            if status == "completed":
                self.history = new_history[-12:] if valid_history else []
                self.history_as_of = result.get("as_of")
                self.input.clear()
            notes = [f"数据截止：{result.get('as_of', '未知')} · 模型：{result.get('model', '已配置模型')}"]
            calls = result.get("tool_calls", [])
            for call in calls[:32] if isinstance(calls, list) else []:
                if isinstance(call, dict):
                    self.activity.appendPlainText(
                        f"{str(call.get('name', '工具'))[:64]} · {str(call.get('status', '未知'))[:24]}"
                        f" · 数据日期 {str(call.get('data_date') or '未提供')[:32]}")
            for source in result.get("sources", []) if isinstance(result.get("sources"), list) else []:
                notes.append("来源：" + (json.dumps(source, ensure_ascii=False) if isinstance(source, dict) else str(source)))
            for warning in result.get("warnings", []) if isinstance(result.get("warnings"), list) else []:
                notes.append("注意：" + str(warning))
            self.messages.append(("assistant", "\n".join(notes)))
            self._render_chat()
            self.status_label.setText("研究完成 · 可追问或开始新对话" if status == "completed" else "研究未完成 · 请缩小问题范围后重试")

    def process_error(self, error):
        if error == QProcess.ProcessError.FailedToStart and not self.cancelled:
            self.task_error = "AI 后台进程未能启动，请检查项目 Python 环境"
            self._end_task()
            self.messages.append(("error", self.task_error))
            self._render_chat()
            self.status_label.setText(self.task_error)

    def finished(self, exit_code, exit_status):
        self.read_stdout()
        self.read_stderr()
        if self.stdout_buffer.strip():
            self.handle_line(self.stdout_buffer.decode("utf-8", errors="replace"))
        self.stdout_buffer = b""
        if self.cancelled:
            message = "研究已停止，问题已保留，可修改后重试"
        elif exit_code or not self.received_result:
            message = self.task_error or "AI 后台任务提前结束，请检查模型设置后重试"
        else:
            message = ""
        if message:
            self.messages.append(("error", message))
            self._render_chat()
            self.status_label.setText(message)
        self._end_task()

    def _end_task(self):
        self.running = False
        process, self.process = self.process, None
        if process:
            process.deleteLater()
        self._sync_controls()
        self.busyChanged.emit(False)

    def cancel(self):
        if self.process and self.running:
            self.cancelled = True
            self.status_label.setText("正在停止研究…")
            self.process.kill()

    def shutdown(self):
        if self.process:
            self.cancelled = True
            self.process.kill()
            self.process.waitForFinished(3000)
