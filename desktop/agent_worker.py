"""Isolated read-only research task; stdin request, UTF-8 JSON Lines stdout."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from desktop.services import Store, clean_json


class WorkerInputError(ValueError):
    """Only fixed, public request validation text may be shown in the UI."""


def emit(event):
    print(json.dumps(clean_json(event), ensure_ascii=False, allow_nan=False), flush=True)


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="StockShape AI research task")
    parser.add_argument("--root", type=Path)
    args = parser.parse_args(argv)
    safe_errors = (WorkerInputError,)
    cancelled_error = None
    try:
        from research_agent.runner import run_agent
        from research_agent.provider import AgentCancelled, ProviderError
        from research_agent.config import ConfigError
        safe_errors = (WorkerInputError, ConfigError, ProviderError)
        cancelled_error = AgentCancelled
        raw = sys.stdin.buffer.readline(131073)
        if len(raw) > 131072:
            raise WorkerInputError("问题和对话上下文过长，请清空对话后重试。")
        try:
            request = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise WorkerInputError("AI 任务请求格式无效") from None
        if not isinstance(request, dict) or set(request) - {"prompt", "history", "as_of"}:
            raise WorkerInputError("AI 任务请求格式无效")

        def progress(event):
            emit({"type": "agent_progress", **{key: event[key] for key in
                 ("phase", "message", "tool", "step") if key in event}})

        result = run_agent(Store(args.root), request.get("prompt", ""),
                           history=request.get("history"), as_of=request.get("as_of"),
                           progress=progress)
        emit({"type": "agent_result", "result": result})
        return 0
    except KeyboardInterrupt:
        emit({"type": "agent_error", "message": "AI 研究已停止"})
        return 130
    except Exception as exc:
        # Only expected, sanitized domain errors may reach the UI. Never print
        # arbitrary transport exceptions, credentials or a request/response body.
        if cancelled_error and isinstance(exc, cancelled_error):
            emit({"type": "agent_error", "message": "AI 研究已停止"})
            return 130
        message = str(exc) if isinstance(exc, safe_errors) else "AI 任务失败，请检查问题格式和模型配置后重试。"
        emit({"type": "agent_error", "message": message})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
