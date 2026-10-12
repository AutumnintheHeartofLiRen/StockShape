"""Subprocess worker: python -m desktop.worker watchlist|all|update.

stdout is UTF-8 JSON Lines only. Analysis runs in this separate process, keeping
the Qt GUI responsive. The update command waits for its child and reports the
actual updater status counts, including per-stock failures.
"""

from __future__ import annotations

import argparse
import codecs
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from .services import AnalysisCancelled, Store, clean_json


def emit(event):
    print(json.dumps(clean_json(event), ensure_ascii=False, allow_nan=False), flush=True)


def update_progress(message):
    match = re.search(r"\]\s*\d+(?:\.\d+)?%\s+(\d+)\s*/\s*(\d+)\b", message)
    if match:
        current, total = map(int, match.groups())
        if 0 <= current <= total and total > 0:
            event = {"type": "progress", "phase": "updating", "current": current,
                     "total": total, "percent": round(100 * current / total, 2), "message": message}
            code = re.search(r"当前\s*[:：]\s*([0-9]{6})(?=\D|$)", message)
            if code:
                event["code"] = code.group(1)
            return event
    return None


def _forward_update_output(output):
    """Relay available bytes without waiting for a newline or a full buffer.

    The updater writes a leading carriage return, then one complete progress
    frame with no trailing newline. Recognize its final failure counter as soon
    as that frame arrives; a later CR/LF still forwards the full text to logs.
    """
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    pending = ""
    last_progress = None

    def send_progress(message):
        nonlocal last_progress
        progress = update_progress(message)
        if progress is not None:
            # A pipe chunk may end partway through the final failure counter.
            # Refresh corrected text for the same stock when the frame grows,
            # while suppressing the identical frame on its following CR/LF.
            key = (progress["current"], progress["total"], progress["message"])
            if key != last_progress:
                emit(progress)
                last_progress = key

    def send_message(message):
        message = message.strip()
        if message:
            emit({"type": "log", "message": message})
            send_progress(message)

    def accept(text):
        nonlocal pending
        pending += text
        frames = re.split(r"[\r\n]", pending)
        pending = frames.pop()
        for frame in frames:
            send_message(frame)
        # Unlike a text-line iterator, this also reports the very first stock
        # while the updater is still busy fetching the next stock.
        if re.search(r"失败\s*[:：]\s*\d+\s*$", pending):
            send_progress(pending.strip())

    # BufferedReader.read1 returns currently available pipe bytes; read(n)
    # can wait for n bytes. The one-byte fallback keeps that guarantee too.
    read = getattr(output, "read1", None)
    while True:
        chunk = read(4096) if read is not None else output.read(1)
        if not chunk:
            break
        accept(decoder.decode(chunk))
    accept(decoder.decode(b"", final=True))
    send_message(pending)


def update(store):
    path = store.root / "update_full_a_daily_v2.py"
    if not path.is_file():
        raise FileNotFoundError(f"找不到日更脚本：{path}")
    total = sum(1 for path in store.stocks_dir.glob("*.csv") if path.is_file())
    emit({"type": "progress", "phase": "preparing", "current": 0, "total": total,
          "percent": 0, "message": "准备更新 / 股票名单与接口预检"})
    command = [sys.executable, "-u", str(path), "--stocks-dir", str(store.stocks_dir),
               "--status", str(store.root / "data/full_a/daily_update_status.csv"),
               "--failed-log", str(store.root / "data/full_a/daily_update_failed.csv")]
    environment = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    process = subprocess.Popen(
        command, cwd=store.root, env=environment, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        assert process.stdout is not None
        _forward_update_output(process.stdout)
        code = process.wait()
        if code:
            raise RuntimeError(f"行情更新进程退出，退出码 {code}；请查看更新日志")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if process.stdout is not None:
            process.stdout.close()
    status = store.market_status()
    if not status["date"]:
        raise RuntimeError(status.get("error", "更新后没有有效市场日期"))
    summary = {**status, "selected": 0, "scope": "update", "success": status["failed"] == 0}
    emit({"type": "result", "rows": [], "summary": summary})


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="StockShape local background worker")
    parser.add_argument("command", choices=["watchlist", "all", "update"])
    parser.add_argument("--root", type=Path)
    args = parser.parse_args(argv)
    store = Store(args.root)
    try:
        if args.command == "update":
            update(store)
        else:
            def progress(current, total, message):
                emit({"type": "progress", "current": current, "total": total, "message": message})
            method = store.analyze_all if args.command == "all" else store.analyze_watchlist
            rows = method(progress=progress)
            emit({"type": "result", "rows": rows, "summary": store.last_summary})
        return 0
    except (AnalysisCancelled, KeyboardInterrupt):
        emit({"type": "error", "message": "任务已取消"})
        return 130
    except Exception as exc:
        emit({"type": "error", "message": f"{type(exc).__name__}：{exc}"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
