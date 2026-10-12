"""External desktop strategy settings, independent of the frozen source files."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import tempfile


class StrategyNotConfigured(ValueError):
    """The local private strategy package or its settings are unavailable."""


def _threshold(value):
    if isinstance(value, bool):
        raise ValueError("RSI6 阈值必须是 1～100 之间的数值")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("RSI6 阈值必须是 1～100 之间的数值") from exc
    if not math.isfinite(number) or not 1 <= number <= 100:
        raise ValueError("RSI6 阈值必须是 1～100 之间的数值")
    return int(number) if number.is_integer() else number


def load_strategy(root) -> dict:
    """Read owner-provided local settings without creating or guessing them."""
    path = Path(root) / "strategy_config.json"
    if not path.is_file():
        raise StrategyNotConfigured("未配置私有策略；请向项目维护者获取策略包与参数。")
    try:
        saved = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(saved, dict) or "rsi6_threshold" not in saved:
            raise ValueError("缺少 rsi6_threshold 字段")
        if saved["rsi6_threshold"] is None:
            raise StrategyNotConfigured("策略参数模板尚未填写；请向项目维护者获取有效参数。")
        config = dict(saved)
        config["version"] = config.get("version", 1)
        config["rsi6_threshold"] = _threshold(config["rsi6_threshold"])
        config["previous_rsi6_threshold"] = _threshold(config.get("previous_rsi6_threshold", config["rsi6_threshold"]))
        config["baseline_rsi6_threshold"] = _threshold(config.get("baseline_rsi6_threshold", config["previous_rsi6_threshold"]))
        config["changes"] = config.get("changes", [])
        if not isinstance(config["changes"], list) or any(not isinstance(item, dict) for item in config["changes"]):
            raise ValueError("changes 必须是变更记录列表")
        return config
    except StrategyNotConfigured:
        raise
    except (ValueError, TypeError) as exc:
        raise ValueError(f"策略配置无法读取，请检查 {path}：{exc}") from exc


def save_rsi6_threshold(root, value) -> dict:
    """Atomically save a threshold, preserving the previous value and history."""
    threshold = _threshold(value)
    config = load_strategy(root)
    previous = config["rsi6_threshold"]
    if threshold != previous:
        config["previous_rsi6_threshold"] = previous
        config["rsi6_threshold"] = threshold
        date = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        config["changes"].append({"date": date, "field": "rsi6_threshold", "from": previous, "to": threshold})
    path = Path(root) / "strategy_config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            json.dump(config, output, ensure_ascii=False, indent=2, allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return config
