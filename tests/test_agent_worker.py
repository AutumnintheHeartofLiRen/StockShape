"""Offline worker protocol integration: synthetic local data and loopback model."""
from __future__ import annotations

import contextlib
import csv
from datetime import date, timedelta
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from desktop import agent_worker
from research_agent.config import AgentConfig, ConfigError, save_agent_config
from research_agent.provider import AgentCancelled, ProviderError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_KEY = "synthetic-worker-test-secret-only"
RAW_FILE_MARKER = "SYNTHETIC_UNUSED_RAW_COLUMN_DO_NOT_SEND"


class WorkerFakeModel:
    """Minimal Chat Completions server that requests bars, then explains them."""

    def __init__(self):
        self.requests = []
        self.evidence = None
        self.failure = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append({"path": self.path, "authorization": self.headers.get("Authorization"), "payload": payload})
                try:
                    if len(owner.requests) == 1:
                        message = {"role": "assistant", "content": None, "tool_calls": [{
                            "id": "synthetic_bars_call", "type": "function", "function": {
                                "name": "get_bars", "arguments": '{"code":"000001","limit":60}',
                            },
                        }]}
                        reason = "tool_calls"
                    else:
                        tool_reply = next(entry for entry in reversed(payload["messages"])
                                          if entry.get("role") == "tool" and entry.get("tool_call_id") == "synthetic_bars_call")
                        owner.evidence = json.loads(tool_reply["content"])
                        data = owner.evidence["data"]
                        answer = (f"合成股票的本地数据截止 {owner.evidence['data_date']}，"
                                  f"收盘价 {data['last_close']}，MA40 为 {data['ma40']}。"
                                  "这是本地日线摘要。")
                        message = {"role": "assistant", "content": answer}
                        reason = "stop"
                    response = {"choices": [{"finish_reason": reason, "message": message}],
                                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}}
                    output = json.dumps(response, ensure_ascii=False).encode("utf-8")
                    self.send_response(200)
                except Exception as error:
                    owner.failure = type(error).__name__
                    output = b'{"error":"synthetic fixture failure"}'
                    self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(output)))
                self.end_headers()
                try:
                    self.wfile.write(output)
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def _tree_snapshot(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


def _isolated_environment(root):
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("AI_")}
    env.update({"PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1", "STOCKSHAPE_ROOT": str(root)})
    return env


class AgentWorkerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def create_data(self):
        stocks = self.root / "data/full_a/stocks"
        stocks.mkdir(parents=True)
        dates = [date(2026, 1, 1) + timedelta(days=index) for index in range(60)]
        cutoff = dates[49].isoformat()
        with (stocks / "000001.csv").open("w", newline="", encoding="utf-8-sig") as output:
            writer = csv.DictWriter(output, fieldnames=["date", "code", "name", "market", "open", "high", "low", "close", "volume", "unused_private_column"])
            writer.writeheader()
            for index, day in enumerate(dates):
                price = 10 + index
                writer.writerow({"date": day.isoformat(), "code": "000001", "name": "合成样本甲", "market": "A", "open": price,
                                 "high": price + 0.5, "low": price - 0.5, "close": price,
                                 "volume": 1000 + index, "unused_private_column": RAW_FILE_MARKER})
        with (stocks.parent / "daily_update_status.csv").open("w", newline="", encoding="utf-8-sig") as output:
            writer = csv.DictWriter(output, fieldnames=["code", "new_last_date", "status", "error"])
            writer.writeheader()
            writer.writerow({"code": "000001", "new_last_date": cutoff, "status": "ok", "error": ""})
        user = self.root / "desktop_data"
        user.mkdir()
        (user / "watchlist.json").write_text(json.dumps({"codes": ["000001"]}), encoding="utf-8")
        return cutoff

    def run_subprocess(self, request):
        return subprocess.run(
            [sys.executable, "-X", "utf8", "-u", "-m", "desktop.agent_worker", "--root", str(self.root)],
            input=(json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=PROJECT_ROOT,
            env=_isolated_environment(self.root), timeout=25, check=False,
        )

    def test_real_worker_utf8_tool_loop_evidence_ma40_and_no_writes(self):
        cutoff = self.create_data()
        with WorkerFakeModel() as server:
            save_agent_config(self.root, AgentConfig(base_url=server.base_url, model="synthetic-tool-model", api_key=SYNTHETIC_KEY))
            before = _tree_snapshot(self.root)
            process = self.run_subprocess({"prompt": "请查询 000001 的本地行情和 MA40。", "history": [], "as_of": cutoff})
            self.assertEqual(process.returncode, 0, process.stderr.decode("utf-8", errors="replace"))
            self.assertEqual(process.stderr, b"")
            output = process.stdout.decode("utf-8")
            events = [json.loads(line) for line in output.splitlines()]
            self.assertTrue(events)
            self.assertTrue(all(isinstance(item, dict) and item.get("type") in {"agent_progress", "agent_result"} for item in events))
            results = [item["result"] for item in events if item["type"] == "agent_result"]
            self.assertEqual(len(results), 1)
            result = results[0]
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["as_of"], cutoff)
            self.assertIn("合成股票", result["answer"])
            self.assertIn("39.5", result["answer"])
            self.assertIn(cutoff, result["answer"])
            self.assertEqual(result["usage"]["total_tokens"], 60)
            self.assertTrue(any(source.get("path") == "data/full_a/stocks/000001.csv" for source in result["sources"]))
            self.assertTrue(any(event.get("tool") == "get_bars" for event in events))
            self.assertEqual(len(server.requests), 2)
            self.assertIsNone(server.failure)
            self.assertEqual([item["path"] for item in server.requests], ["/v1/chat/completions"] * 2)
            self.assertTrue(all(item["authorization"] == "Bearer " + SYNTHETIC_KEY for item in server.requests))
            self.assertEqual(server.evidence["data_date"], cutoff)
            self.assertEqual(server.evidence["data"]["total_bars"], 50)
            self.assertEqual(server.evidence["data"]["last_close"], 59)
            self.assertEqual(server.evidence["data"]["ma40"], 39.5)
            self.assertTrue(all(bar["date"] <= cutoff for bar in server.evidence["data"]["bars"]))
            for item in server.requests:
                serialized = json.dumps(item["payload"], ensure_ascii=False)
                self.assertNotIn(SYNTHETIC_KEY, serialized)
                self.assertNotIn(RAW_FILE_MARKER, serialized)
            self.assertNotIn(SYNTHETIC_KEY, output)
            self.assertNotIn(RAW_FILE_MARKER, output)
            self.assertEqual(_tree_snapshot(self.root), before)
            self.assertEqual({path.name for path in (self.root / "desktop_data").iterdir()}, {"watchlist.json"})

    def test_real_worker_missing_configuration_reports_without_network_or_files(self):
        with WorkerFakeModel() as server:
            before = _tree_snapshot(self.root)
            process = self.run_subprocess({"prompt": "本地数据状态怎样？"})
            self.assertEqual(process.returncode, 1)
            self.assertEqual(process.stderr, b"")
            events = [json.loads(line) for line in process.stdout.decode("utf-8").splitlines()]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["type"], "agent_error")
            self.assertIn("模型设置", events[0]["message"])
            self.assertIn("服务地址", events[0]["message"])
            self.assertEqual(server.requests, [])
            self.assertEqual(_tree_snapshot(self.root), before)
            self.assertFalse((self.root / ".env.local").exists())


class AgentWorkerErrorTests(unittest.TestCase):
    def run_main(self, raw, run_agent_error=None, run_agent_result=None, root=None):
        output = io.StringIO()
        fake_stdin = SimpleNamespace(buffer=io.BytesIO(raw))
        arguments = [] if root is None else ["--root", str(root)]
        with mock.patch("sys.stdin", fake_stdin), contextlib.redirect_stdout(output):
            with mock.patch("research_agent.runner.run_agent", side_effect=run_agent_error, return_value=run_agent_result) as run:
                exit_code = agent_worker.main(arguments)
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        return exit_code, events, output.getvalue(), run

    def test_unexpected_value_error_and_other_errors_are_redacted(self):
        for error in (ValueError("synthetic-private-secret"), RuntimeError("synthetic-private-secret"), OSError("synthetic-private-secret")):
            with self.subTest(error=type(error).__name__):
                code, events, output, _ = self.run_main(b'{"prompt":"test"}\n', run_agent_error=error)
                self.assertEqual(code, 1)
                self.assertEqual(events[0]["type"], "agent_error")
                self.assertNotIn("synthetic-private-secret", output)
                self.assertIn("检查问题格式和模型配置", events[0]["message"])

    def test_sanitized_configuration_and_provider_errors_remain_helpful(self):
        for error in (ConfigError("请求超时必须在 1～180 秒之间。"), ProviderError("模型服务拒绝认证，请检查 API Key 和账户权限。")):
            with self.subTest(error=type(error).__name__):
                code, events, _, _ = self.run_main(b'{"prompt":"test"}\n', run_agent_error=error)
                self.assertEqual(code, 1)
                self.assertEqual(events, [{"type": "agent_error", "message": str(error)}])

    def test_cancellation_and_keyboard_interrupt_report_stop(self):
        for error in (AgentCancelled("synthetic-private-secret"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                code, events, output, _ = self.run_main(b'{"prompt":"test"}\n', run_agent_error=error)
                self.assertEqual(code, 130)
                self.assertIn("停止", events[0]["message"])
                self.assertNotIn("synthetic-private-secret", output)

    def test_invalid_json_utf8_non_object_and_unknown_fields_are_fixed_errors(self):
        for raw in (b'not-json synthetic-private-secret\n', b'\xffsynthetic-private-secret\n', b'[]\n', b'null\n', b'{"prompt":"test","api_key":"synthetic-private-secret"}\n'):
            with self.subTest(raw=raw):
                code, events, output, run = self.run_main(raw)
                self.assertEqual(code, 1)
                self.assertEqual(events, [{"type": "agent_error", "message": "AI 任务请求格式无效"}])
                self.assertNotIn("synthetic-private-secret", output)
                run.assert_not_called()

    def test_oversized_stdin_rejected_before_agent_or_provider(self):
        code, events, output, run = self.run_main(b'x' * 131073)
        self.assertEqual(code, 1)
        self.assertIn("上下文过长", events[0]["message"])
        self.assertLess(len(output), 200)
        run.assert_not_called()

    def test_missing_config_never_constructs_network_opener(self):
        with tempfile.TemporaryDirectory() as directory:
            fake_stdin = SimpleNamespace(buffer=io.BytesIO(b'{"prompt":"synthetic test"}\n'))
            output = io.StringIO()
            with mock.patch.dict(os.environ, _isolated_environment(directory), clear=True), mock.patch("sys.stdin", fake_stdin), contextlib.redirect_stdout(output), mock.patch("research_agent.provider.build_opener") as opener:
                code = agent_worker.main(["--root", directory])
            self.assertEqual(code, 1)
            opener.assert_not_called()
            self.assertIn("模型设置", output.getvalue())
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
