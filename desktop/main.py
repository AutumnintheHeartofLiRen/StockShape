"""Personal stock research desktop UI. Long tasks run in a separate process."""
from __future__ import annotations

from datetime import datetime
import codecs
import json
import os
from pathlib import Path
import sys
import traceback

from PySide6.QtCore import QLockFile, QProcess, QProcessEnvironment, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QIcon
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFrame, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QSplitter, QTableWidget,
    QTableWidgetItem, QTabWidget, QToolButton, QVBoxLayout, QWidget,
)

from desktop.chart import CandlestickChart
from desktop.agent_panel import AgentPanel
from desktop.services import ROOT, Store


STYLE = """
QWidget { font-family: 'Microsoft YaHei UI'; font-size: 13px; color: #25334a; }
QMainWindow, QWidget#canvas { background: #f3f5f9; }
QFrame#panel, QFrame#sidebar { background: white; border: 1px solid #e2e7ef; border-radius: 12px; }
QLabel#title { font-size: 24px; font-weight: 700; color: #172743; }
QLabel#subtitle, QLabel#muted { color: #78869c; font-size: 12px; }
QLabel#section { font-size: 14px; font-weight: 600; }
QLabel#pill { color: #42639a; background: #eaf0fb; padding: 8px 13px; border-radius: 8px; }
QLabel#stockTitle { font-size: 20px; font-weight: 600; }
QLabel#price { font-size: 24px; font-weight: 600; }
QLineEdit { background: #f7f9fc; border: 1px solid #dfe5ee; border-radius: 7px; padding: 9px; selection-background-color: #d9e6ff; }
QLineEdit:focus { border-color: #729ce3; }
QPushButton { background: white; border: 1px solid #dce3ed; padding: 9px 15px; border-radius: 7px; font-weight: 500; }
QPushButton:hover { background: #f0f5fd; border-color: #94b3e3; }
QPushButton#primary { background: #306bdc; color: white; border: 1px solid #306bdc; }
QPushButton#primary:hover { background: #245bc5; }
QPushButton:disabled { color: #a3adba; background: #f0f2f5; border-color: #e2e7ee; }
QPushButton#primary:disabled { color: #a3adba; background: #e6edf8; border-color: #e2e7ee; }
QToolButton { border: 0; color: #6580aa; padding: 5px; }
QListWidget { background: transparent; border: 0; outline: 0; }
QListWidget::item { padding: 10px 7px; border-radius: 6px; margin: 2px 0; }
QListWidget::item:selected { color: #235fc9; background: #eaf1ff; }
QListWidget::item:hover { background: #f1f5fb; }
QTabWidget::pane { border: 0; background: white; }
QTabBar::tab { padding: 10px 18px; color: #78869c; border-bottom: 2px solid transparent; }
QTabBar::tab:selected { color: #2867d6; border-bottom-color: #2867d6; font-weight: 600; }
QTableWidget { border: 0; gridline-color: #f0f2f6; background: white; alternate-background-color: #fafbfe; selection-background-color: #eaf1ff; selection-color: #25334a; }
QHeaderView::section { background: #f7f9fc; border: 0; border-bottom: 1px solid #e5eaf1; padding: 8px 5px; color: #7b8799; font-size: 12px; }
QProgressBar { background: #e8edf5; border: 0; border-radius: 3px; height: 6px; }
QProgressBar::chunk { background: #4280e4; border-radius: 3px; }
QPlainTextEdit { background: #f8fafd; border: 1px solid #e2e7ef; border-radius: 6px; font-family: Consolas, 'Microsoft YaHei UI'; font-size: 11px; }
QComboBox { padding: 5px 10px; background: white; border: 1px solid #dfe5ee; border-radius: 6px; }
QCheckBox { color: #66748c; }
QSplitter::handle { background: transparent; height: 9px; width: 12px; }
QScrollBar:vertical { width: 7px; background: transparent; }
QScrollBar::handle:vertical { background: #dce3ec; border-radius: 3px; min-height: 25px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""


def panel(name="panel"):
    widget = QFrame()
    widget.setObjectName(name)
    return widget


def label(text, name="muted"):
    widget = QLabel(text)
    widget.setObjectName(name)
    return widget


def numeric(value, digits=2):
    try:
        number = float(value)
        return f"{number:.{digits}f}" if number == number else "—"
    except (ValueError, TypeError):
        return "—"


class MainWindow(QMainWindow):
    def __init__(self, store=None):
        super().__init__()
        self.store = store or Store()
        self.root = Path(getattr(self.store, "root", ROOT))
        self.universe = self.store.load_universe()
        self.names = {row["code"]: row for row in self.universe}
        self.strategy_config = {}
        try:
            self.strategy_config = self.store.strategy()
        except (OSError, ValueError) as exc:
            self.strategy_error = str(exc)
        self.analysis_state = self.store.analysis_availability()
        self.watch_rows = {}
        self.all_rows = []
        self.watch_threshold = None
        self.all_threshold = None
        self.current_code = None
        self.current_bars = None
        self.task = None
        self.process = None
        self.stdout_buffer = b""
        self.stderr_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.received_result = False
        self.cancelled = False
        self.task_error = ""
        self.setWindowTitle("StockShape · 个人股票研究 V1.4")
        self.setMinimumSize(1060, 740)
        self.resize(1450, 940)
        self.setStyleSheet(STYLE)
        icon = Path(__file__).parent / "assets/stockshape.svg"
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))
        self._build_ui()
        self._load_saved_analysis()
        self.refresh_watchlist()
        self.refresh_status()
        if not self.analysis_state["available"]:
            self.status_label.setText(self.analysis_state["reason"])
        self.search_stocks("")
        if not self.store.watchlist() and self.all_rows:
            self.tabs.setCurrentIndex(1)
        first = self.all_rows[0]["code"] if self.all_rows else (self.universe[0]["code"] if self.universe else None)
        if first:
            self.show_stock(first)

    def _build_ui(self):
        canvas = QWidget()
        canvas.setObjectName("canvas")
        self.setCentralWidget(canvas)
        outer = QVBoxLayout(canvas)
        outer.setContentsMargins(22, 18, 22, 14)
        outer.setSpacing(14)
        header = QHBoxLayout()
        brand = label("S", "title")
        brand.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand.setFixedSize(44, 44)
        brand.setStyleSheet("background:#306bdc;color:white;border-radius:12px;font-size:27px;font-weight:700;")
        header.addWidget(brand)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        titles.addWidget(label("StockShape", "title"))
        titles.addWidget(label("个人股票研究 · 日线与形态分析", "subtitle"))
        header.addLayout(titles)
        header.addStretch()
        header.addWidget(label("RSI6 小于"))
        self.rsi_combo = QComboBox()
        self.rsi_combo.setObjectName("rsiThreshold")
        threshold = self.strategy_config.get("rsi6_threshold")
        baseline = self.strategy_config.get("baseline_rsi6_threshold")
        if threshold is not None:
            previous = self.strategy_config.get("previous_rsi6_threshold")
            for value, suffix in [(threshold, "（原值）" if threshold == baseline else ""),
                                  (baseline, "（原值）"), (previous, "（上次值）")]:
                if value is not None and self.rsi_combo.findData(value) < 0:
                    self.rsi_combo.addItem(f"{value:g}{suffix}", value)
        else:
            self.rsi_combo.addItem("未配置", None)
        self.rsi_combo.setEnabled(self.analysis_state["available"])
        self.rsi_combo.setToolTip("从本地策略文件读取参数；更换后需重新分析，自选列表会保留。")
        self.rsi_combo.currentIndexChanged.connect(self.change_rsi_threshold)
        header.addWidget(self.rsi_combo)
        header.addSpacing(12)
        self.data_badge = label("读取本地行情…", "pill")
        header.addWidget(self.data_badge)
        outer.addLayout(header)

        body = QHBoxLayout()
        body.setSpacing(16)
        sidebar = panel("sidebar")
        sidebar.setFixedWidth(245)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(15, 18, 15, 16)
        side.setSpacing(10)
        side.addWidget(label("搜索股票", "section"))
        self.search = QLineEdit()
        self.search.setObjectName("stockSearch")
        self.search.setPlaceholderText("股票代码 / 名称")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.search_stocks)
        side.addWidget(self.search)
        self.search_hint = label("输入代码或名称，从本地股票库搜索")
        self.search_hint.setWordWrap(True)
        side.addWidget(self.search_hint)
        self.search_results = QListWidget()
        self.search_results.setObjectName("searchResults")
        self.search_results.setMaximumHeight(176)
        self.search_results.itemClicked.connect(lambda item: self.show_stock(item.data(Qt.ItemDataRole.UserRole)))
        self.search_results.itemDoubleClicked.connect(lambda item: self.add_watch(item.data(Qt.ItemDataRole.UserRole)))
        side.addWidget(self.search_results)
        self.add_button = QPushButton("＋ 加入自选")
        self.add_button.setObjectName("addWatch")
        self.add_button.clicked.connect(self.add_search_stock)
        side.addWidget(self.add_button)
        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet("background:#e8edf4; margin-top:8px;")
        side.addWidget(divider)
        watch_header = QHBoxLayout()
        self.watch_title = label("自选股  0", "section")
        watch_header.addWidget(self.watch_title)
        watch_header.addStretch()
        remove = QToolButton()
        remove.setText("移除")
        remove.setObjectName("removeWatch")
        remove.setToolTip("移除当前选中的自选股")
        remove.clicked.connect(self.remove_watch)
        watch_header.addWidget(remove)
        self.remove_button = remove
        side.addLayout(watch_header)
        self.watch_list = QListWidget()
        self.watch_list.setObjectName("watchlist")
        self.watch_list.itemClicked.connect(lambda item: self.show_stock(item.data(Qt.ItemDataRole.UserRole)))
        side.addWidget(self.watch_list, 1)
        self.watch_empty = label("搜索后加入股票，建立你的观察池")
        self.watch_empty.setWordWrap(True)
        side.addWidget(self.watch_empty)
        side.addWidget(label("数据保存在本机 · 非复权日线"))
        body.addWidget(sidebar)

        market_page = QWidget()
        workspace = QVBoxLayout(market_page)
        workspace.setContentsMargins(0, 0, 0, 0)
        workspace.setSpacing(12)
        actions = QHBoxLayout()
        self.coverage_label = label("—")
        actions.addWidget(self.coverage_label)
        actions.addStretch()
        self.update_button = QPushButton("更新本地数据")
        self.update_button.setObjectName("updateData")
        self.update_button.clicked.connect(lambda: self.start_task("update"))
        self.watch_button = QPushButton("分析自选股")
        self.watch_button.setObjectName("analyzeWatchlist")
        self.watch_button.clicked.connect(lambda: self.start_task("watchlist"))
        self.all_button = QPushButton("分析全部股票")
        self.all_button.setObjectName("primary")
        self.all_button.clicked.connect(lambda: self.start_task("all"))
        self.all_button.setEnabled(self.analysis_state["available"])
        for button in [self.update_button, self.watch_button, self.all_button]:
            actions.addWidget(button)
        workspace.addLayout(actions)

        self.task_panel = panel()
        task_layout = QVBoxLayout(self.task_panel)
        task_layout.setContentsMargins(16, 12, 16, 12)
        task_layout.setSpacing(7)
        task_header = QHBoxLayout()
        self.task_title = label("任务进度", "section")
        self.task_percent = label("0.0%", "pill")
        task_header.addWidget(self.task_title)
        task_header.addStretch()
        task_header.addWidget(self.task_percent)
        task_layout.addLayout(task_header)
        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("taskProgress")
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%v / %m 只")
        self.progress_bar.setMinimumHeight(20)
        self.progress_bar.setStyleSheet("QProgressBar {height:20px;background:#e8edf5;color:#28476b;text-align:center;border-radius:5px;} QProgressBar::chunk {background:#7ea7ee;border-radius:5px;}")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        task_layout.addWidget(self.progress_bar)
        self.task_detail = label("等待任务开始")
        self.task_detail.setWordWrap(True)
        task_layout.addWidget(self.task_detail)
        self.task_panel.hide()
        workspace.addWidget(self.task_panel)

        splitter = QSplitter(Qt.Orientation.Vertical)
        chart_panel = panel()
        chart_layout = QVBoxLayout(chart_panel)
        chart_layout.setContentsMargins(16, 14, 16, 10)
        stock_head = QHBoxLayout()
        ticker = QVBoxLayout()
        ticker.setSpacing(3)
        self.stock_title = label("选择一只股票", "stockTitle")
        self.stock_meta = label("点击搜索结果、自选股或分析结果查看 K 线")
        ticker.addWidget(self.stock_title)
        ticker.addWidget(self.stock_meta)
        stock_head.addLayout(ticker)
        stock_head.addStretch()
        self.price_label = label("—", "price")
        self.change_label = label("—")
        stock_head.addWidget(self.price_label)
        stock_head.addWidget(self.change_label)
        stock_head.addSpacing(18)
        self.range_combo = QComboBox()
        for name, value in [("近 120 日", 120), ("近 250 日", 250), ("近 500 日", 500), ("全部历史", 0)]:
            self.range_combo.addItem(name, value)
        self.range_combo.setCurrentIndex(1)
        self.range_combo.currentIndexChanged.connect(self.change_range)
        stock_head.addWidget(self.range_combo)
        reset = QToolButton()
        reset.setText("重置视图")
        reset.clicked.connect(lambda: self.chart.reset_view())
        stock_head.addWidget(reset)
        chart_layout.addLayout(stock_head)
        self.chart = CandlestickChart()
        chart_layout.addWidget(self.chart, 1)
        self.reason_label = label("选择股票后可运行形态分析；搜索和查看 K 线无需联网。")
        self.reason_label.setWordWrap(True)
        self.reason_label.setMinimumHeight(36)
        chart_layout.addWidget(self.reason_label)
        splitter.addWidget(chart_panel)

        results_panel = panel()
        results_layout = QVBoxLayout(results_panel)
        results_layout.setContentsMargins(12, 4, 12, 10)
        self.tabs = QTabWidget()
        self.watch_table = self.make_table(["代码", "名称", "分析结论", "涨跌幅", "分析日期", "说明"])
        self.all_table = self.make_table(["代码", "名称", "分析结论", "涨跌幅", "分析日期", "说明"])
        self.tabs.addTab(self.watch_table, "自选股分析")
        all_page = QWidget()
        all_layout = QVBoxLayout(all_page)
        all_layout.setContentsMargins(0, 6, 0, 0)
        filter_row = QHBoxLayout()
        self.all_summary = label("尚未运行全市场分析")
        self.all_summary.setWordWrap(True)
        filter_row.addWidget(self.all_summary, 1)
        self.only_selected = QCheckBox("只看入选")
        self.only_selected.setChecked(True)
        self.only_selected.toggled.connect(self.refresh_all_table)
        filter_row.addWidget(self.only_selected)
        self.result_filter = QLineEdit()
        self.result_filter.setPlaceholderText("筛选代码 / 名称")
        self.result_filter.setFixedWidth(170)
        self.result_filter.textChanged.connect(self.refresh_all_table)
        filter_row.addWidget(self.result_filter)
        all_layout.addLayout(filter_row)
        all_layout.addWidget(self.all_table)
        self.tabs.addTab(all_page, "全市场结果")
        results_layout.addWidget(self.tabs)
        splitter.addWidget(results_panel)
        splitter.setSizes([520, 255])
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        workspace.addWidget(splitter, 1)
        self.workspace_tabs = QTabWidget()
        self.workspace_tabs.addTab(market_page, "行情与选股")
        self.agent_panel = AgentPanel(self.store, self)
        self.workspace_tabs.addTab(self.agent_panel, "AI 研究助手")
        self.agent_panel.busyChanged.connect(self.agent_busy_changed)
        body.addWidget(self.workspace_tabs, 1)
        outer.addLayout(body, 1)

        footer = QHBoxLayout()
        self.status_label = label("就绪 · 分析使用本地数据和当前形态规则")
        footer.addWidget(self.status_label, 1)
        self.cancel_button = QToolButton()
        self.cancel_button.setText("停止分析")
        self.cancel_button.clicked.connect(self.cancel_task)
        self.cancel_button.hide()
        footer.addWidget(self.cancel_button)
        log_toggle = QToolButton()
        log_toggle.setText("运行日志")
        log_toggle.setCheckable(True)
        log_toggle.toggled.connect(lambda visible: self.log_view.setVisible(visible))
        footer.addWidget(log_toggle)
        outer.addLayout(footer)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(1500)
        self.log_view.setFixedHeight(108)
        self.log_view.hide()
        outer.addWidget(self.log_view)

    def make_table(self, headers):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setAlternatingRowColors(True)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        table.verticalHeader().hide()
        table.verticalHeader().setDefaultSectionSize(34)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        table.horizontalHeader().setStretchLastSection(True)
        for index, width in enumerate([80, 100, 112, 85, 100, 260]):
            table.setColumnWidth(index, width)
        table.itemSelectionChanged.connect(lambda: self.table_selected(table))
        table.setSortingEnabled(True)
        return table

    def search_stocks(self, text):
        query = text.strip().casefold()
        self.search_results.clear()
        if not query:
            self.search_hint.setText("输入代码或名称，从本地股票库搜索")
            self.add_button.setEnabled(False)
            return
        found = [row for row in self.universe if query in row["code"].casefold() or query in row["name"].casefold()]
        self.search_hint.setText(f"找到 {len(found)} 只股票" + (" · 请细化关键词" if len(found) > 30 else " · 双击加入自选"))
        for row in found[:30]:
            item = QListWidgetItem(f'{row["code"]}    {row["name"]}')
            item.setData(Qt.ItemDataRole.UserRole, row["code"])
            self.search_results.addItem(item)
        if found:
            self.search_results.setCurrentRow(0)
        self.add_button.setEnabled(bool(found) and not self.task and not self.agent_panel.running)

    def add_search_stock(self):
        item = self.search_results.currentItem()
        if item:
            self.add_watch(item.data(Qt.ItemDataRole.UserRole))

    def add_watch(self, code):
        if self.task or self.agent_panel.running:
            return
        try:
            self.store.add_watch(code)
            self.refresh_watchlist()
            self.show_stock(code)
            self.status_label.setText(f"已加入自选：{code} {self.names.get(code, {}).get('name', '')}")
        except Exception as exc:
            self.show_error(str(exc))

    def remove_watch(self):
        item = self.watch_list.currentItem()
        if item and not self.task and not self.agent_panel.running:
            try:
                code = item.data(Qt.ItemDataRole.UserRole)
                self.store.remove_watch(code)
                self.refresh_watchlist()
                self.status_label.setText(f"已移出自选：{code}")
            except Exception as exc:
                self.show_error(str(exc))

    def refresh_watchlist(self):
        codes = self.store.watchlist()
        self.watch_list.clear()
        for code in codes:
            item = QListWidgetItem(f"{code}    {self.names.get(code, {}).get('name', '')}")
            item.setData(Qt.ItemDataRole.UserRole, code)
            self.watch_list.addItem(item)
        self.watch_title.setText(f"自选股  {len(codes)}")
        self.watch_empty.setVisible(not codes)
        self.watch_button.setEnabled(bool(codes) and not self.task and not self.agent_panel.running and self.analysis_state["available"])
        rows = [self.watch_rows.get(code, {"code": code, "name": self.names.get(code, {}).get("name", ""), "verdict": "待分析", "reason": "点击「分析自选股」计算当前形态"}) for code in codes]
        self.fill_table(self.watch_table, rows)

    def refresh_status(self):
        try:
            status = self.store.market_status()
            date = status.get("date") or "未知"
            total = status.get("total", len(self.universe))
            covered = status.get("covered", 0)
            self.data_badge.setText(f"本地行情  {date}")
            self.agent_panel.set_market_date(status.get("date"))
            self.coverage_label.setText(f"{total:,} 只本地股票 · 当日覆盖 {covered:,} 只")
        except Exception as exc:
            self.data_badge.setText("行情状态需检查")
            self.coverage_label.setText(str(exc))

    def change_rsi_threshold(self, *_):
        if self.task or self.agent_panel.running or not self.analysis_state["available"]:
            return
        value = self.rsi_combo.currentData()
        try:
            self.strategy_config = self.store.set_rsi6_threshold(value)
            self.set_all_summary()
            self.update_reason()
            self.status_label.setText(f"已保存 RSI6 < {value:g} · 请重新分析自选或全部股票")
        except Exception as exc:
            self.rsi_combo.blockSignals(True)
            self.rsi_combo.setCurrentIndex(self.rsi_combo.findData(self.strategy_config["rsi6_threshold"]))
            self.rsi_combo.blockSignals(False)
            self.show_error(str(exc))

    @staticmethod
    def result_threshold(summary, legacy=None):
        if not isinstance(summary, dict):
            return legacy
        strategy = summary.get("strategy")
        nested = strategy.get("rsi6_threshold", legacy) if isinstance(strategy, dict) else legacy
        try:
            value = float(summary.get("rsi6_threshold", nested))
            return value if 1 <= value <= 100 else legacy
        except (ValueError, TypeError):
            return legacy

    def _load_saved_analysis(self):
        for scope in ["watchlist", "all"]:
            path = Path(self.store.user_dir) / f"analysis_{scope}.json"
            if path.is_file():
                try:
                    saved = json.loads(path.read_text(encoding="utf-8-sig"))
                    rows = saved if isinstance(saved, list) else saved.get("rows", [])
                    summary = {} if isinstance(saved, list) else saved.get("summary", {})
                    if scope == "watchlist":
                        self.watch_rows = {row["code"]: row for row in rows}
                        self.watch_threshold = self.result_threshold(summary, self.strategy_config.get("baseline_rsi6_threshold"))
                    else:
                        self.all_rows = rows
                        self.all_threshold = self.result_threshold(summary, self.strategy_config.get("baseline_rsi6_threshold"))
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    self.append_log(f"无法读取上次分析：{exc}")
        if not self.all_rows:
            try:
                rows, manifest = self.store.latest_candidates()
                self.all_rows = [{**row, "selected": True, "verdict": "已入选", "reason": row.get("进入原因", "冻结模型候选"), "change_pct": None} for row in rows]
                self.all_threshold = self.strategy_config.get("baseline_rsi6_threshold")
                self.set_all_summary()
            except Exception as exc:
                self.append_log(f"读取冻结结果：{exc}")
        else:
            self.set_all_summary()
        self.refresh_all_table()

    def set_all_summary(self):
        count = sum(bool(row.get("selected")) for row in self.all_rows)
        date = max((str(row.get("date", "")) for row in self.all_rows), default="")
        threshold = self.all_threshold
        parameter = f"RSI6 < {threshold:g}" if threshold is not None else "参数未记录"
        previous = " · 旧参数，请重新分析" if threshold is not None and threshold != self.strategy_config.get("rsi6_threshold") else ""
        self.all_summary.setText(f"{date} · {len(self.all_rows):,} 只 · 入选 {count} 只 · {parameter}{previous}")

    def refresh_all_table(self, *_):
        rows = self.all_rows
        if self.only_selected.isChecked():
            rows = [row for row in rows if row.get("selected")]
        query = self.result_filter.text().strip().casefold()
        if query:
            rows = [row for row in rows if query in f"{row.get('code', '')} {row.get('name', '')}".casefold()]
        self.fill_table(self.all_table, rows)

    def fill_table(self, table, rows):
        table.blockSignals(True)
        table.setSortingEnabled(False)
        table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            change = row.get("change_pct")
            change_text = f"{float(change):+.2f}%" if change is not None and numeric(change) != "—" else "—"
            values = [row.get("code", ""), row.get("name", ""), row.get("verdict", "—"), change_text, row.get("date", "—"), row.get("reason", "")]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.ItemDataRole.UserRole, row.get("code"))
                item.setToolTip(str(row.get("reason", "")))
                if column == 2 and row.get("selected"):
                    item.setForeground(QColor("#306bdc"))
                if column == 3 and change is not None:
                    try:
                        item.setForeground(QColor("#e7505a" if float(change) > 0 else "#159b75" if float(change) < 0 else "#78869c"))
                    except (TypeError, ValueError):
                        pass
                table.setItem(index, column, item)
        table.setSortingEnabled(True)
        table.blockSignals(False)

    def table_selected(self, table):
        items = table.selectedItems()
        if items:
            self.show_stock(items[0].data(Qt.ItemDataRole.UserRole))

    def show_stock(self, code):
        if not code:
            return
        try:
            bars = self.store.load_bars(code)
            self.current_code = code
            self.current_bars = bars
            name = self.names.get(code, {}).get("name", "")
            self.agent_panel.set_current_stock(code, name)
            self.stock_title.setText(f"{name}  {code}")
            self.stock_meta.setText(f"{self.names.get(code, {}).get('market', 'A 股')} · 非复权日 K · {len(bars):,} 个交易日")
            if not bars.empty:
                close = float(bars.iloc[-1]["close"])
                change = (close / float(bars.iloc[-2]["close"]) - 1) * 100 if len(bars) > 1 and float(bars.iloc[-2]["close"]) else 0
                color = "#e7505a" if change > 0 else "#159b75" if change < 0 else "#25334a"
                self.price_label.setText(f"{close:.2f}")
                self.price_label.setStyleSheet(f"color:{color};font-size:24px;font-weight:600;")
                self.change_label.setText(f"{change:+.2f}%")
                self.change_label.setStyleSheet(f"color:{color};")
                self.stock_meta.setText(self.stock_meta.text() + f" · 行情截至 {bars.iloc[-1]['date']:%Y-%m-%d}")
            else:
                self.price_label.setText("—")
                self.change_label.setText("—")
            self.change_range()
            self.update_reason()
        except Exception as exc:
            self.append_log(f"读取股票 {code} 失败：{exc}")
            self.status_label.setText(f"无法读取 {code} 的行情：{exc}")

    def update_reason(self):
        row = self.watch_rows.get(self.current_code)
        threshold = self.watch_threshold
        if not row:
            row = next((row for row in self.all_rows if row.get("code") == self.current_code), None)
            threshold = self.all_threshold
        if row:
            note = f"RSI6 < {threshold:g}" if threshold is not None else ""
            if threshold is not None and threshold != self.strategy_config.get("rsi6_threshold"):
                note += "（旧参数，需重新分析）"
            self.reason_label.setText(f"{row.get('date', '')} · {note} · {row.get('verdict', '分析结果')}  |  {row.get('reason', '')}")
        else:
            self.reason_label.setText("尚未分析该股票。加入自选后点击「分析自选股」，查看是否满足现有形态规则。")

    def change_range(self, *_):
        if self.current_bars is not None:
            self.chart.set_bars(self.current_bars, self.current_code or "", self.names.get(self.current_code, {}).get("name", ""), int(self.range_combo.currentData()))

    def start_task(self, command):
        if self.task:
            return
        if self.agent_panel.running:
            self.status_label.setText("AI 正在读取本地研究数据，请等待结束或先停止 AI 研究。")
            return
        if command in {"watchlist", "all"} and not self.analysis_state["available"]:
            self.status_label.setText(self.analysis_state["reason"])
            return
        if command == "watchlist" and not self.store.watchlist():
            self.status_label.setText("请先搜索股票并加入自选")
            return
        python = self.root / ".venv/Scripts/python.exe"
        if not python.is_file():
            self.show_error("未找到项目 Python 环境，请按维护说明重建 .venv。")
            return
        self.task = command
        self.cancelled = False
        self.received_result = False
        self.task_error = ""
        self.stdout_buffer = b""
        self.stderr_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.set_busy(True)
        names = {"update": "更新本地数据", "watchlist": "分析自选股", "all": "分析全部股票"}
        self.status_label.setText(f"正在{names[command]}…")
        self.append_log(f"开始：{names[command]}")
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
        self.process.finished.connect(self.task_finished)
        self.process.errorOccurred.connect(self.process_error)
        self.process.start(str(python), ["-X", "utf8", "-u", "-m", "desktop.worker", command])

    def set_busy(self, busy):
        blocked = busy or self.agent_panel.running
        self.agent_panel.set_external_busy(busy)
        for button in [self.update_button, self.all_button, self.add_button, self.remove_button]:
            button.setEnabled(not blocked)
        self.all_button.setEnabled(not blocked and self.analysis_state["available"])
        self.watch_button.setEnabled(not blocked and bool(self.store.watchlist()) and self.analysis_state["available"])
        self.add_button.setEnabled(not blocked and self.search_results.count() > 0)
        self.cancel_button.setVisible(busy and self.task != "update")
        self.rsi_combo.setEnabled(not blocked and self.analysis_state["available"])
        if busy:
            self.task_panel.show()
            total = len(self.store.watchlist()) if self.task == "watchlist" else len(self.universe)
            self.progress_bar.setRange(0, max(total, 1))
            self.progress_bar.setValue(0)
            self.task_percent.setText("0.0%")
            titles = {"update": "本地数据更新", "watchlist": "自选股分析", "all": "全市场分析"}
            self.task_title.setText(titles.get(self.task, "任务进度"))
            self.task_detail.setText("正在获取股票名单并预检接口，尚未开始逐股更新…" if self.task == "update" else f"正在准备分析 · RSI6 < {self.strategy_config['rsi6_threshold']}")

    def read_stdout(self):
        if not self.process:
            return
        self.stdout_buffer += bytes(self.process.readAllStandardOutput())
        while b"\n" in self.stdout_buffer:
            line, self.stdout_buffer = self.stdout_buffer.split(b"\n", 1)
            self.handle_event_line(line.decode("utf-8", errors="replace").strip())

    def handle_event_line(self, line):
        if not line:
            return
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            self.append_log(line)
            return
        if not isinstance(event, dict):
            self.append_log(f"忽略无法识别的任务消息：{line[:200]}")
            return
        kind = event.get("type")
        if kind == "progress":
            try:
                total = max(0, int(event.get("total", 0)))
                current = max(0, min(total, int(event.get("current", 0))))
            except (ValueError, TypeError):
                self.append_log("忽略无效的任务进度消息")
                return
            if total:
                self.progress_bar.setRange(0, total)
                self.progress_bar.setValue(current)
                self.task_percent.setText(f"{100 * current / total:.1f}%")
            self.task_detail.setText(str(event.get("message", "处理中")))
            if event.get("phase") == "preparing":
                self.task_percent.setText("准备中")
            message = "正在更新本地数据" if self.task == "update" else str(event.get("message", "处理中"))
            self.status_label.setText(message + (f" · {current}/{total}" if total else ""))
        elif kind == "log":
            self.append_log(str(event.get("message", "")))
        elif kind == "error":
            self.task_error = str(event.get("message", "运行失败"))
            self.append_log(self.task_error)
        elif kind == "result":
            if self.cancelled:
                return
            rows = event.get("rows", [])
            summary = event.get("summary", {})
            if not isinstance(rows, list) or not isinstance(summary, dict) or any(
                    not isinstance(row, dict) or not isinstance(row.get("code"), str)
                    or len(row["code"]) != 6 or not row["code"].isdigit() for row in rows):
                self.append_log("忽略格式无效的分析结果消息")
                return
            self.received_result = True
            if self.task == "watchlist":
                self.watch_rows = {row["code"]: row for row in rows}
                self.watch_threshold = self.result_threshold(summary, self.strategy_config["rsi6_threshold"])
                self.refresh_watchlist()
                self.tabs.setCurrentIndex(0)
            elif self.task == "all":
                self.all_rows = rows
                self.all_threshold = self.result_threshold(summary, self.strategy_config["rsi6_threshold"])
                self.set_all_summary()
                self.refresh_all_table()
                self.tabs.setCurrentIndex(1)
            elif self.task == "update":
                self.universe = self.store.load_universe(refresh=True)
                self.names = {row["code"]: row for row in self.universe}
                self.refresh_status()
                self.refresh_watchlist()
                self.search_stocks(self.search.text())
                if self.current_code:
                    self.show_stock(self.current_code)
            self.update_reason()
            failed = summary.get("failed", 0)
            if self.task == "update":
                message = f"更新结束 · 行情截至 {summary.get('date', '见状态')} · 失败 {failed} 只；分析需单独运行"
            else:
                selected = sum(bool(row.get("selected")) for row in rows)
                message = f"分析完成 · {len(rows):,} 只 · 入选 {selected} 只" + (f" · 异常 {failed} 只" if failed else "")
            self.status_label.setText(message)
            self.task_title.setText("更新结束" if self.task == "update" else "分析完成")
            self.task_detail.setText(message)
            self.append_log(message)

    def read_stderr(self):
        if self.process:
            message = self.stderr_decoder.decode(bytes(self.process.readAllStandardError())).strip()
            if message:
                self.append_log(message)

    def process_error(self, error):
        if self.cancelled:
            return
        if error == QProcess.ProcessError.FailedToStart:
            self.task_error = self.process.errorString()
            process = self.process
            self.process = None
            self.task = None
            self.set_busy(False)
            self.task_title.setText("任务未能启动")
            self.task_percent.setText("失败")
            self.task_detail.setText(self.task_error)
            process.deleteLater()
            self.show_error(self.task_error)

    def task_finished(self, exit_code, exit_status):
        self.read_stdout()
        self.read_stderr()
        if self.stdout_buffer.strip():
            self.handle_event_line(self.stdout_buffer.decode("utf-8", errors="replace").strip())
        if self.cancelled:
            self.status_label.setText("分析已停止 · 上次完整结果仍保留")
            self.task_title.setText("分析已停止")
            self.task_detail.setText("上次完整结果仍保留")
        elif exit_code or not self.received_result:
            message = self.task_error or f"进程提前结束（退出码 {exit_code}），请查看运行日志"
            self.status_label.setText(message)
            self.task_title.setText("任务失败")
            self.task_detail.setText(message)
            self.show_error(message)
        self.task = None
        self.set_busy(False)
        if self.received_result and not self.cancelled and exit_code == 0:
            self.progress_bar.setValue(self.progress_bar.maximum())
            self.task_percent.setText("100.0%")
        if self.process:
            self.process.deleteLater()
            self.process = None

    def cancel_task(self):
        if self.process and self.task != "update":
            self.cancelled = True
            self.cancel_button.setEnabled(False)
            self.process.kill()
            self.cancel_button.setEnabled(True)

    def append_log(self, message):
        if message:
            self.log_view.appendPlainText(f"{datetime.now():%H:%M:%S}  {message}")

    def show_error(self, message):
        self.append_log(message)
        QMessageBox.warning(self, "StockShape", message)

    def agent_busy_changed(self, busy):
        self.set_busy(bool(self.task))
        if busy:
            self.status_label.setText("AI 正在研究 · 本地更新和策略修改暂时锁定")
        elif not self.task:
            self.status_label.setText("AI 任务已结束 · 可以继续查看行情或更新数据")

    def closeEvent(self, event):
        if self.task == "update":
            QMessageBox.information(self, "更新正在进行", "数据正在逐股写入，请等待更新结束后关闭窗口。")
            event.ignore()
            return
        if self.task:
            answer = QMessageBox.question(self, "分析正在进行", "停止本次分析并退出？上次完整结果会保留。")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.cancelled = True
            self.process.kill()
            self.process.waitForFinished(3000)
        self.agent_panel.shutdown()
        event.accept()


def main(root=ROOT):
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("StockShape")
    app.setOrganizationName("StockShape")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    user_dir = Path(root) / "desktop_data"
    user_dir.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(user_dir / "app.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        QMessageBox.information(None, "StockShape 已打开", "桌面程序已在运行，请切换到已有窗口。")
        return 0
    try:
        window = MainWindow(Store(root))
    except Exception:
        detail = traceback.format_exc()
        (user_dir / "startup_error.log").write_text(detail, encoding="utf-8")
        QMessageBox.critical(None, "StockShape 启动失败", "请查看 desktop_data/startup_error.log。\n" + detail.splitlines()[-1])
        return 1
    screen = app.primaryScreen().availableGeometry()
    window.resize(min(window.width(), screen.width() - 60), min(window.height(), screen.height() - 70))
    window.show()
    result = app.exec()
    lock.unlock()
    return result
