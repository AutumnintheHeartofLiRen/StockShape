"""Offline updater stream tests: real framing, mocked child process only."""
from __future__ import annotations

import io
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from desktop import worker


def progress_line(current, total=2, code="000001", name="示例甲银行", failed=0):
    return (f"[██--] {100 * current / total:6.2f}% {current}/{total} "
            f"当前:{code} {name} 已用:00:02 ETA:00:03 "
            f"更新:{current} 无新增:0 失败:{failed}")


class ChunkedOutput:
    def __init__(self, chunks, before_read=None):
        self.chunks = list(chunks)
        self.before_read = before_read
        self.index = 0
        self.closed = False

    def read1(self, size):
        if self.before_read:
            self.before_read(self.index)
        if self.index >= len(self.chunks):
            return b""
        chunk = self.chunks[self.index]
        self.index += 1
        return chunk

    def close(self):
        self.closed = True


class FakeProcess:
    def __init__(self, output, exit_code=0, terminate_timeout=False):
        self.stdout = output
        self.exit_code = exit_code
        self.returncode = None
        self.waits = []
        self.terminated = False
        self.killed = False
        self.terminate_timeout = terminate_timeout

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if timeout is not None and self.terminate_timeout:
            raise subprocess.TimeoutExpired("offline updater", timeout)
        self.returncode = self.exit_code
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class UpdateProgressTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        stocks = root / "data/full_a/stocks"
        stocks.mkdir(parents=True)
        for code in ["000001", "000002"]:
            (stocks / f"{code}.csv").write_text("offline fixture\n", encoding="utf-8")
        (root / "update_full_a_daily_v2.py").write_text("# mocked; never executed\n", encoding="utf-8")
        self.market = {"date": "2026-09-30", "total": 2, "covered": 2,
                       "coverage": 1.0, "failed": 0}
        self.store = SimpleNamespace(root=root, stocks_dir=stocks,
                                     market_status=lambda: dict(self.market))
        self.events = []

    def run_update(self, process, popen=None):
        factory = popen or (lambda *args, **kwargs: process)
        with patch.object(worker.subprocess, "Popen", side_effect=factory) as create, \
                patch.object(worker, "emit", side_effect=self.events.append):
            worker.update(self.store)
        return create

    def updates(self):
        return [event for event in self.events if event.get("phase") == "updating"]

    def test_parse_progress_fields_and_optional_current_code(self):
        event = worker.update_progress(progress_line(1))
        self.assertEqual(event["current"], 1)
        self.assertEqual(event["total"], 2)
        self.assertEqual(event["percent"], 50.0)
        self.assertEqual(event["code"], "000001")
        self.assertEqual(event["phase"], "updating")
        without_code = worker.update_progress("[----] 33.33% 1/3 正在处理")
        self.assertNotIn("code", without_code)
        self.assertEqual(without_code["percent"], 33.33)
        for message in ["预检通过 000001", "[--] 50% 3/2", "[--] 0% 0/0", "[--] nan% 1/2"]:
            with self.subTest(message=message):
                self.assertIsNone(worker.update_progress(message))

    def test_preparing_emitted_before_child_without_fake_processed_stocks(self):
        output = ChunkedOutput(["股票名单已刷新\n接口预检通过\r\n".encode("utf-8")])
        process = FakeProcess(output)

        def start(*args, **kwargs):
            self.assertEqual(self.events, [{"type": "progress", "phase": "preparing", "current": 0,
                                           "total": 2, "percent": 0,
                                           "message": "准备更新 / 股票名单与接口预检"}])
            self.assertNotIn("text", kwargs)
            self.assertNotIn("encoding", kwargs)
            self.assertIn("-u", args[0])
            return process

        self.run_update(process, start)
        self.assertEqual(self.updates(), [])
        self.assertTrue(output.closed)
        self.assertEqual(self.events[-1]["type"], "result")

    def test_carriage_return_frame_emitted_before_next_read_or_process_exit(self):
        first = "股票名单刷新完成\r\n\r" + progress_line(1)
        second = "\r" + progress_line(2, code="000002", name="示例乙") + "\r\n更新完成\n"
        process = FakeProcess(None)

        def before_read(index):
            if index == 1:
                self.assertEqual([event["current"] for event in self.updates()], [1])
                self.assertEqual(process.waits, [])
                self.assertIsNone(process.poll())

        output = ChunkedOutput([first.encode("utf-8"), second.encode("utf-8")], before_read)
        process.stdout = output
        self.run_update(process)
        self.assertEqual([event["current"] for event in self.updates()], [1, 2])
        self.assertEqual([event["code"] for event in self.updates()], ["000001", "000002"])
        self.assertEqual(self.updates()[-1]["percent"], 100)
        self.assertEqual(process.waits, [None])
        self.assertFalse(process.terminated)
        self.assertTrue(output.closed)

    def test_utf8_split_every_byte_mixed_delimiters_and_unterminated_final_frame(self):
        lines = ["接口预检：行情连接正常", progress_line(1), progress_line(2, code="000002", name="示例乙")]
        payload = (lines[0] + "\r\n\r" + lines[1] + "\r" + lines[2]).encode("utf-8")
        output = ChunkedOutput([payload[index:index + 1] for index in range(len(payload))])
        self.run_update(FakeProcess(output))
        logs = [event["message"] for event in self.events if event["type"] == "log"]
        self.assertEqual(logs, lines)
        self.assertEqual([event["current"] for event in self.updates()], [1, 2])
        self.assertFalse(any("\ufffd" in event.get("message", "") for event in self.events))
        self.assertEqual(self.updates()[-1]["message"], lines[-1])

    def test_split_failure_counter_refreshes_message_and_deduplicates_complete_frame(self):
        partial = progress_line(1, failed=1)
        complete = progress_line(1, failed=12)

        def before_read(index):
            if index == 1:
                # The first unterminated frame is visible before another read.
                self.assertEqual([event["message"] for event in self.updates()], [partial])

        output = ChunkedOutput([("\r" + partial).encode("utf-8"),
                                b"2\r", (complete + "\r\n").encode("utf-8")], before_read)
        self.run_update(FakeProcess(output))
        updates = self.updates()
        self.assertEqual([event["message"] for event in updates], [partial, complete])
        self.assertEqual([(event["current"], event["total"]) for event in updates], [(1, 2), (1, 2)])

    def test_pipe_without_read1_uses_single_byte_nonfilling_fallback(self):
        class PlainPipe:
            def __init__(self, payload):
                self.buffer = io.BytesIO(payload)
                self.closed = False

            def read(inner, size):
                self.assertEqual(size, 1)
                return inner.buffer.read(size)

            def close(inner):
                inner.closed = True
                inner.buffer.close()

        output = PlainPipe(("\r" + progress_line(1)).encode("utf-8"))
        self.run_update(FakeProcess(output))
        self.assertEqual(self.updates()[0]["current"], 1)
        self.assertTrue(output.closed)

    def test_result_uses_real_failed_status_after_reaching_100_percent(self):
        self.market["failed"] = 1
        output = ChunkedOutput([("\r" + progress_line(2, failed=0)).encode("utf-8")])
        self.run_update(FakeProcess(output))
        self.assertEqual(self.updates()[-1]["percent"], 100)
        self.assertEqual(self.events[-1]["summary"]["failed"], 1)
        self.assertFalse(self.events[-1]["summary"]["success"])

    def test_nonzero_child_exit_raises_and_cannot_emit_success_result(self):
        output = ChunkedOutput(["连接失败\n".encode("utf-8")])
        process = FakeProcess(output, exit_code=3)
        with self.assertRaisesRegex(RuntimeError, "退出码 3"):
            self.run_update(process)
        self.assertFalse(any(event["type"] == "result" for event in self.events))
        self.assertTrue(output.closed)
        self.assertEqual(process.waits, [None])

    def test_stream_error_terminates_and_reaps_child_with_kill_fallback(self):
        class BrokenOutput(ChunkedOutput):
            def read1(self, size):
                raise OSError("offline broken pipe")

        output = BrokenOutput([])
        process = FakeProcess(output, terminate_timeout=True)
        with self.assertRaisesRegex(OSError, "offline broken pipe"):
            self.run_update(process)
        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertEqual(process.waits, [5, None])
        self.assertTrue(output.closed)
        self.assertFalse(any(event["type"] == "result" for event in self.events))

    def test_invalid_final_market_status_cannot_emit_result(self):
        self.market.update(date="", error="离线状态缺失")
        with self.assertRaisesRegex(RuntimeError, "离线状态缺失"):
            self.run_update(FakeProcess(ChunkedOutput([])))
        self.assertFalse(any(event["type"] == "result" for event in self.events))


if __name__ == "__main__":
    unittest.main()
