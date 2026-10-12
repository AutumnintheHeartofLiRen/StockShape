"""Local data services with an optional, separately installed strategy provider.

Progress callbacks receive (current, total, message). Cancellation may be a
threading.Event or a zero-argument callable; it is checked between stocks and
model stages. AnalysisCancelled prevents saving a partial analysis as complete.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Callable

import numpy as np
import pandas as pd

from .strategy import StrategyNotConfigured, load_strategy, save_rsi6_threshold


ROOT = Path(os.environ.get("STOCKSHAPE_ROOT") or Path(__file__).resolve().parents[1]).resolve()
MIN_COVERAGE = 0.95  # Local data freshness, independent of strategy selection.


class AnalysisCancelled(RuntimeError):
    """The caller cancelled analysis; no completed result was saved."""


def clean_json(value):
    """Convert pandas/numpy values to strict JSON, using null for missing data."""
    if isinstance(value, dict):
        return {str(k): clean_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_json(v) for v in value]
    if isinstance(value, (pd.Timestamp,)):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, np.generic):
        return clean_json(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is pd.NA or value is pd.NaT:
        return None
    return value


def _atomic_text(path: Path, text: str, encoding="utf-8"):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding=encoding, newline="", dir=path.parent,
            prefix=path.name + ".", suffix=".tmp", delete=False,
        ) as output:
            temporary = Path(output.name)
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载模块：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _code(value) -> str:
    value = str(value).strip()
    if not re.fullmatch(r"[0-9]{1,6}", value):
        raise ValueError("股票代码必须为 1～6 位数字")
    return value.zfill(6)


def _status_code(value) -> str:
    """Normalize provider status codes, including exchange prefixes and .0."""
    value = re.sub(r"\.0$", "", str(value).strip())
    match = re.search(r"([0-9]{6})", value)
    return match.group(1) if match else value.zfill(6)


def _cancelled(cancel):
    if cancel is None:
        return
    requested = cancel() if callable(cancel) else cancel.is_set()
    if requested:
        raise AnalysisCancelled("分析已取消，未保存本轮结果")


class Store:
    def __init__(self, root=None):
        self.root = Path(root).resolve() if root is not None else ROOT
        self.stocks_dir = self.root / "data" / "full_a" / "stocks"
        self.user_dir = self.root / "desktop_data"
        self._universe = None
        self._metadata = {}
        self.last_summary = {}

    def strategy(self) -> dict:
        return load_strategy(self.root)

    def set_rsi6_threshold(self, value) -> dict:
        return save_rsi6_threshold(self.root, value)

    def load_universe(self, refresh=False) -> list[dict]:
        """Local metadata; fresh update names override the older stock list.

        Missing metadata is read from the first row of its stock CSV only.
        refresh=True invalidates the cache after a successful data update.
        """
        if refresh:
            self._universe = None
        if self._universe is None:
            metadata = {}
            for filename in ["stock_list.csv", "daily_update_status.csv"]:
                path = self.stocks_dir.parent / filename
                if not path.is_file():
                    continue
                try:
                    with path.open(encoding="utf-8-sig", newline="") as handle:
                        for item in csv.DictReader(handle):
                            value = str(item.get("code", "")).strip()
                            if not re.fullmatch(r"[0-9]{1,6}", value):
                                continue
                            code = value.zfill(6)
                            entry = metadata.setdefault(code, {})
                            for field in ["name", "market"]:
                                if item.get(field, "").strip():
                                    entry[field] = item[field].strip()
                except (OSError, UnicodeError, csv.Error):
                    pass
            rows = []
            for path in sorted(self.stocks_dir.glob("*.csv")):
                if not re.fullmatch(r"[0-9]{6}", path.stem):
                    continue
                entry = metadata.get(path.stem, {})
                name, market = entry.get("name", ""), entry.get("market", "")
                if not name or not market:
                    try:
                        with path.open(encoding="utf-8-sig", newline="") as handle:
                            first = next(csv.DictReader(handle), {})
                        name = name or str(first.get("name", "")).strip()
                        market = market or str(first.get("market", "")).strip()
                    except (OSError, UnicodeError, csv.Error):
                        # A damaged file remains searchable; analysis reports it.
                        pass
                rows.append({"code": path.stem, "name": name, "market": market})
            self._universe = rows
            self._metadata = {row["code"]: row for row in rows}
        return [dict(row) for row in self._universe]

    def load_bars(self, code) -> pd.DataFrame:
        code = _code(code)
        path = self.stocks_dir / f"{code}.csv"
        if not path.is_file():
            raise FileNotFoundError(f"本地没有 {code} 的行情文件")
        frame = pd.read_csv(path, dtype={"code": str}, encoding="utf-8-sig")
        required = ["date", "open", "high", "low", "close", "volume"]
        missing = set(required) - set(frame.columns)
        if missing:
            raise ValueError(f"{code} 行情缺少字段：{', '.join(sorted(missing))}")
        frame["date"] = pd.to_datetime(frame["date"], format="mixed", errors="coerce").dt.normalize()
        numeric = required[1:]
        for field in numeric:
            frame[field] = pd.to_numeric(frame[field], errors="coerce")
        valid = frame["date"].notna() & np.isfinite(frame[numeric].to_numpy(dtype=float)).all(axis=1)
        valid &= frame[["open", "high", "low", "close"]].gt(0).all(axis=1)
        valid &= frame["volume"].ge(0)
        valid &= frame["high"].ge(frame[["open", "close", "low"]].max(axis=1))
        valid &= frame["low"].le(frame[["open", "close", "high"]].min(axis=1))
        invalid_ohlcv = int((frame["date"].notna() & ~valid).sum())
        duplicate_dates = int(frame.loc[frame["date"].notna(), "date"].duplicated().sum())
        frame = frame.loc[valid].sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
        if frame.empty:
            raise ValueError(f"{code} 没有有效的日线记录")
        frame.attrs["invalid_ohlcv_rows"] = invalid_ohlcv
        frame.attrs["duplicate_dates"] = duplicate_dates
        return frame

    def watchlist(self) -> list[str]:
        path = self.user_dir / "watchlist.json"
        if not path.exists():
            return []
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            codes = saved.get("codes") if isinstance(saved, dict) else saved
            if not isinstance(codes, list):
                raise ValueError("缺少股票代码列表")
            return list(dict.fromkeys(_code(code) for code in codes))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"自选文件无法读取，请检查 {path}：{exc}") from exc

    def _save_watchlist(self, codes):
        text = json.dumps({"version": 1, "codes": codes}, ensure_ascii=False, indent=2)
        _atomic_text(self.user_dir / "watchlist.json", text + "\n")

    def add_watch(self, code):
        code = _code(code)
        if not (self.stocks_dir / f"{code}.csv").is_file():
            raise ValueError(f"本地股票库中不存在 {code}")
        codes = self.watchlist()
        if code not in codes:
            codes.append(code)
            self._save_watchlist(codes)
        return codes

    def remove_watch(self, code):
        code = _code(code)
        # Permit removing a stock whose data file was subsequently removed.
        codes = [item for item in self.watchlist() if item != code]
        self._save_watchlist(codes)
        return codes

    def market_status(self) -> dict:
        """Find the latest date covered by the local market data quality policy."""
        path = self.root / "data" / "full_a" / "daily_update_status.csv"
        result = {"date": "", "total": 0, "covered": 0, "coverage": 0.0, "failed": 0}
        if not path.is_file():
            return {**result, "error": "缺少日更状态文件，请先更新本地行情"}
        try:
            frame = pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")
            date_col = next((c for c in ["new_last_date", "last_date", "latest_date", "date"] if c in frame), None)
            if date_col is None or frame.empty:
                raise ValueError("日更状态中没有有效日期字段或记录")
            dates = pd.to_datetime(frame[date_col], format="mixed", errors="coerce").dt.normalize()
            code_col = next((c for c in ["code", "股票代码", "symbol"] if c in frame), None)
            if code_col:
                codes = frame[code_col].map(_status_code)
                dated = pd.DataFrame({"code": codes, "date": dates}).dropna(subset=["date"])
                last_dates = dated.groupby("code")["date"].max()
            else:
                last_dates = dates.dropna()
            total = len(last_dates) if code_col else len(frame)
            state = frame.get("status", pd.Series("", index=frame.index)).str.lower()
            errors = frame.get("error", pd.Series("", index=frame.index)).str.strip()
            failed_mask = state.str.contains(r"fail|error", regex=True) | errors.ne("")
            failed = int(frame.loc[failed_mask, code_col].nunique()) if code_col else int(failed_mask.sum())
            result.update(total=int(total), failed=failed, statuses=state.value_counts().to_dict())
            if total <= 0 or last_dates.empty:
                raise ValueError("日更状态中没有有效股票日期")
            minimum = MIN_COVERAGE
            for day in sorted(last_dates.unique(), reverse=True):
                covered = int((last_dates >= day).sum())
                if covered / total >= minimum:
                    result.update(date=pd.Timestamp(day).strftime("%Y-%m-%d"), covered=covered,
                                  coverage=covered / total, min_coverage=minimum,
                                  raw_latest_date=pd.Timestamp(last_dates.max()).strftime("%Y-%m-%d"))
                    return clean_json(result)
            raise ValueError(f"没有交易日达到 {minimum:.0%} 的数据覆盖率")
        except (ValueError, OSError, KeyError) as exc:
            return {**result, "error": str(exc)}

    def latest_candidates(self) -> tuple[list[dict], dict]:
        directory = self.root / "frozen_model_daily" / "latest"
        manifest_path = directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
        path = directory / "current_candidates.csv"
        readable_path = directory / "current_candidates_readable.csv"
        readable = {}
        if readable_path.is_file():
            frame = pd.read_csv(readable_path, dtype={"code": str}, encoding="utf-8-sig")
            readable = {row["code"]: row for row in frame.to_dict("records")}
        if path.is_file():
            frame = pd.read_csv(path, dtype={"code": str}, encoding="utf-8-sig")
            # Preserve numeric baseline fields for regression and give the UI
            # the existing, detailed Chinese entry reason on first launch.
            rows = [{**row, **readable.get(row["code"], {})} for row in frame.to_dict("records")]
        else:
            rows = list(readable.values())
        for row in rows:
            row.update(selected=True, verdict="进入观察池", reason=row.get("进入原因", "历史候选"))
        return clean_json(rows), manifest

    def analysis_availability(self) -> dict:
        """Check configuration and provider presence without executing either."""
        path = self.root / "private" / "strategy_engine.py"
        if not path.is_file():
            return {"available": False, "reason": "未安装本地策略模块；行情、自选和 K 线仍可使用。"}
        try:
            self.strategy()
        except (StrategyNotConfigured, ValueError, OSError) as exc:
            return {"available": False, "reason": str(exc)}
        return {"available": True, "reason": "本地策略模块已配置"}

    def _strategy_provider(self):
        availability = self.analysis_availability()
        if not availability["available"]:
            raise StrategyNotConfigured(availability["reason"])
        path = self.root / "private" / "strategy_engine.py"
        try:
            module = _load_module(path, "stockshape_private_strategy")
            factory = getattr(module, "create_engine", None)
            if not callable(factory):
                raise ValueError("策略模块缺少 create_engine(store) 工厂")
            provider = factory(self)
            for name in ("verify_sources", "analyze_codes"):
                if not callable(getattr(provider, name, None)):
                    raise ValueError(f"策略模块缺少 {name} 方法")
            return provider
        except StrategyNotConfigured:
            raise
        except Exception as exc:
            raise StrategyNotConfigured(f"本地策略模块无法加载：{exc}") from exc

    def verify_frozen_sources(self) -> dict:
        return self._strategy_provider().verify_sources()

    def analyze_codes(self, codes, progress: Callable | None = None, cancel=None) -> list[dict]:
        _cancelled(cancel)
        return self._strategy_provider().analyze_codes(codes, progress=progress, cancel=cancel)

    def _save_analysis(self, label, rows):
        summary = dict(self.last_summary)
        summary["scope"] = label
        json_path = self.user_dir / f"analysis_{label}.json"
        csv_path = self.user_dir / f"analysis_{label}.csv"
        flat = [{**row, "metrics": json.dumps(row["metrics"], ensure_ascii=False, allow_nan=False)} for row in rows]
        columns = ["code", "name", "date", "close", "change_pct", "selected", "verdict", "reason", "metrics", "status"]
        _atomic_text(csv_path, pd.DataFrame(flat, columns=columns).to_csv(index=False), encoding="utf-8-sig")
        _atomic_text(json_path, json.dumps({"rows": rows, "summary": summary}, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        self.last_summary = summary
        return rows

    def analyze_watchlist(self, progress=None, cancel=None):
        rows = self.analyze_codes(self.watchlist(), progress=progress, cancel=cancel)
        return self._save_analysis("watchlist", rows)

    def analyze_all(self, progress=None, cancel=None):
        codes = [path.stem for path in sorted(self.stocks_dir.glob("*.csv"))
                 if re.fullmatch(r"[0-9]{6}", path.stem)]
        if not codes:
            raise ValueError("本地股票库为空，未运行全市场分析；上次结果已保留")
        rows = self.analyze_codes(codes, progress=progress, cancel=cancel)
        return self._save_analysis("all", rows)
