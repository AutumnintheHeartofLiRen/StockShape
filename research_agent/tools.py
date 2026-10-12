"""Bounded, read-only local evidence for the research assistant.

Only the functions below are callable by a model. The tools never load a
strategy provider, calculate a private signal, update data, or expose a private
rule. Saved analysis is evidence for its own trading day, not historical replay.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import math
import re

import pandas as pd

from desktop.services import Store, clean_json


_CODE = re.compile(r"[0-9]{6}\Z")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_STATUS = {"ok", "stale", "insufficient", "error"}
_ANALYSIS_FILES = ("analysis_watchlist.json", "analysis_all.json")


def _day(value: str) -> str:
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise ValueError("日期必须使用 YYYY-MM-DD 格式")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError("日期无效") from None


def _maybe_day(value):
    try:
        return _day(value)
    except (TypeError, ValueError):
        return None


def _code(value) -> str:
    if not isinstance(value, str) or not _CODE.fullmatch(value):
        raise ValueError("股票代码必须是 6 位数字字符串")
    return value


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        numeric = float(value)
        return numeric if math.isfinite(numeric) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _label(value, limit=64):
    # Metadata remains untrusted text. Never pass unrestricted file contents.
    return str(value or "").replace("\x00", "")[:limit]


def _function(name, description, properties=None, required=()):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties or {},
                       "required": list(required), "additionalProperties": False},
    }}


class ResearchTools:
    """Local evidence snapshot, pinned to one explicit trading day."""

    def __init__(self, store: Store, as_of: str | None = None):
        self.store = store
        try:
            self._market_snapshot = dict(store.market_status())
        except Exception:
            self._market_snapshot = {}
        fallback = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        self.as_of = _day(as_of) if as_of is not None else (
            _maybe_day(self._market_snapshot.get("date")) or fallback)
        self._analysis_snapshot = None

    def schemas(self) -> list[dict]:
        code = {"type": "string", "pattern": "^[0-9]{6}$", "description": "6 位股票代码"}
        return [
            _function("search_stocks", "按代码或名称搜索本地股票目录，不联网。", {
                "query": {"type": "string", "minLength": 1, "maxLength": 100,
                          "description": "股票代码的一部分或股票名称"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20,
                          "description": "最多返回数量，默认 10"},
            }, ("query",)),
            _function("get_market_status", "查看任务日期对应的本地行情覆盖情况和数据日期。"),
            _function("get_watchlist", "读取用户已经保存的自选股票，不增删自选。"),
            _function("get_bars", "读取截至任务日期的有限日线、涨跌幅和 MA5/10/20/40；不包含未来行情。", {
                "code": code,
                "limit": {"type": "integer", "minimum": 1, "maximum": 60,
                          "description": "最多返回日线数量，默认 30"},
            }, ("code",)),
            _function("get_analysis", "读取任务交易日已经保存的本地策略判定，不执行模型，不公开私有规则。", {
                "code": code,
            }, ("code",)),
            _function("get_candidates", "读取任务交易日已经保存的候选股票，不计算新结果。", {
                "limit": {"type": "integer", "minimum": 1, "maximum": 30,
                          "description": "最多返回数量，默认 20"},
            }),
        ]

    def toolschema(self, name):
        return next((tool for tool in self.schemas() if tool["function"]["name"] == name), None)

    def _result(self, status="ok", data=None, data_date=None, sources=None, warnings=None):
        return clean_json({"status": status, "data": data, "as_of": self.as_of,
                           "data_date": data_date, "strategy_version": None,
                           "sources": sources or [], "warnings": list(dict.fromkeys(warnings or []))})

    def execute(self, name: str, arguments: dict) -> dict:
        schema = self.toolschema(name)
        if schema is None:
            return self._result("error", {"message": "不支持的只读工具"})
        if not isinstance(arguments, dict):
            return self._result("error", {"message": "工具参数必须是 JSON 对象"})
        parameters = schema["function"]["parameters"]
        if set(arguments) - set(parameters["properties"]):
            return self._result("error", {"message": "工具参数包含未允许的字段"})
        if set(parameters["required"]) - set(arguments):
            return self._result("error", {"message": "工具缺少必要参数"})
        try:
            if "code" in arguments:
                _code(arguments["code"])
            if "limit" in arguments:
                limit = arguments["limit"]
                maximum = parameters["properties"]["limit"]["maximum"]
                if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= maximum:
                    raise ValueError(f"limit 必须是 1～{maximum} 的整数")
            if "query" in arguments:
                query = arguments["query"]
                if not isinstance(query, str) or not 1 <= len(query.strip()) <= 100:
                    raise ValueError("搜索词必须是 1～100 个字符")
        except ValueError as exc:
            # Only fixed public validation text is returned to the caller.
            return self._result("error", {"message": str(exc)})
        try:
            return getattr(self, "_" + name)(**arguments)
        except Exception:
            return self._result("error", {"message": "本地数据无法读取，请在桌面检查数据文件"})

    def _metadata(self):
        return {row["code"]: {"code": row["code"], "name": _label(row.get("name")),
                              "market": _label(row.get("market"), 20)}
                for row in self.store.load_universe()
                if isinstance(row, dict) and isinstance(row.get("code"), str)
                and _CODE.fullmatch(row["code"])}

    def _search_stocks(self, query, limit=10):
        query = query.strip()
        rows = [row for row in self._metadata().values()
                if query.casefold() in (row["code"] + " " + row["name"]).casefold()]
        return self._result(data={"query": query, "stocks": rows[:limit], "count": len(rows),
                                  "returned": min(limit, len(rows))},
                            sources=[{"path": "data/full_a/stocks", "date": None}])

    def _get_market_status(self):
        snapshot = self._market_snapshot
        latest = _maybe_day(snapshot.get("date"))
        source = [{"path": "data/full_a/daily_update_status.csv", "date": latest}]
        if not latest:
            return self._result("missing", {"message": "本地行情状态缺失或无效"}, sources=source)
        if latest > self.as_of:
            return self._result("missing", {"message": "当前状态文件不能还原任务历史日的覆盖情况"},
                                sources=[], warnings=["行情状态晚于任务日期，未使用未来状态"])
        fields = ("total", "covered", "coverage", "failed", "min_coverage")
        data = {field: _number(snapshot.get(field)) for field in fields}
        data["date"] = latest
        warnings = ["本地行情未覆盖任务日期，请先在桌面更新"] if latest < self.as_of else []
        return self._result(data=data, data_date=latest, sources=source, warnings=warnings)

    def _get_watchlist(self):
        try:
            codes = [_code(code) for code in self.store.watchlist()]
        except (ValueError, OSError):
            return self._result("error", {"message": "自选文件无法读取，请在桌面检查自选列表"})
        metadata = self._metadata()
        stocks = [metadata.get(code, {"code": code, "name": "", "market": ""}) for code in codes[:200]]
        warnings = ["自选超过 200 只，本次仅返回前 200 只"] if len(codes) > 200 else []
        return self._result(data={"stocks": stocks, "count": len(codes), "returned": len(stocks)},
                            sources=[{"path": "desktop_data/watchlist.json", "date": None}], warnings=warnings)

    def _get_bars(self, code, limit=30):
        try:
            frame = self.store.load_bars(code)
        except FileNotFoundError:
            return self._result("missing", {"code": code, "message": "本地没有该股票行情"})
        except (ValueError, OSError, pd.errors.ParserError):
            return self._result("error", {"code": code, "message": "行情文件无法读取或没有有效日线"})
        warnings = []
        if frame.attrs.get("invalid_ohlcv_rows", 0):
            warnings.append("行情中存在异常 OHLCV 记录，已排除；请先核对本地数据")
        if frame.attrs.get("duplicate_dates", 0):
            warnings.append("行情中存在重复日期，已保留每个日期的最后有效记录")
        # Historical queries must be truncated before indicators are calculated.
        frame = frame.loc[frame["date"] <= pd.Timestamp(self.as_of)].copy()
        if frame.empty:
            return self._result("missing", {"code": code, "message": "截至任务日期没有有效行情"}, warnings=warnings)
        for period in (5, 10, 20, 40):
            frame[f"ma{period}"] = frame["close"].rolling(period).mean()
        last = frame.iloc[-1]
        latest = last["date"].strftime("%Y-%m-%d")
        if latest < self.as_of:
            warnings.append("该股票行情早于任务日期，可能停牌或尚未更新")
        change = None
        if len(frame) >= 2:
            change = (float(last["close"]) / float(frame.iloc[-2]["close"]) - 1) * 100
        else:
            warnings.append("仅一条日线，无法计算日涨跌幅")
        if len(frame) < 40:
            warnings.append("历史不足 40 个有效交易日，MA40 暂不可用")
        columns = ["date", "open", "high", "low", "close", "volume", "ma5", "ma10", "ma20", "ma40"]
        visible = frame[columns].tail(limit)
        data = {"code": code, "name": self._metadata().get(code, {}).get("name", ""),
                "bars": visible.to_dict("records"), "count": len(visible), "total_bars": len(frame),
                "range": {"start": visible.iloc[0]["date"].strftime("%Y-%m-%d"), "end": latest},
                "last_close": last["close"], "change_pct": change,
                **{f"ma{period}": last[f"ma{period}"] for period in (5, 10, 20, 40)}}
        return self._result(data=data, data_date=latest,
                            sources=[{"path": f"data/full_a/stocks/{code}.csv", "code": code, "date": latest}],
                            warnings=warnings)

    def _load_analysis(self):
        if self._analysis_snapshot is not None:
            return self._analysis_snapshot
        snapshots, warnings = [], []
        for filename in _ANALYSIS_FILES:
            path = self.store.user_dir / filename
            if not path.is_file():
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(value, dict) or not isinstance(value.get("rows"), list):
                    raise ValueError("unsupported cache")
                summary = value.get("summary")
                if not isinstance(summary, dict):
                    raise ValueError("unsupported summary")
                snapshot_day = _maybe_day(summary.get("date"))
                if not snapshot_day:
                    warnings.append("分析缓存缺少有效交易日，未用于研究")
                    continue
                if snapshot_day != self.as_of:
                    warnings.append("分析缓存晚于任务日期，未使用未来结果" if snapshot_day > self.as_of
                                    else "分析缓存早于任务日期，请在桌面重新分析")
                    continue
                # Internal comparison detects incompatible cached configurations;
                # this value and all private metadata are never emitted.
                private_version = json.dumps({"strategy": summary.get("strategy"),
                                              "rsi6_threshold": summary.get("rsi6_threshold")},
                                             sort_keys=True, ensure_ascii=False)
                snapshots.append({"rows": value["rows"], "date": snapshot_day,
                                  "path": f"desktop_data/{filename}", "mtime": path.stat().st_mtime_ns,
                                  "private_version": private_version})
            except (OSError, UnicodeError, ValueError, TypeError):
                warnings.append("有分析缓存损坏或格式不兼容，已跳过")
        snapshots.sort(key=lambda item: item["mtime"], reverse=True)
        self._analysis_snapshot = snapshots, warnings
        return self._analysis_snapshot

    @staticmethod
    def _public_analysis(row, snapshot_day):
        selected = row.get("selected") if isinstance(row.get("selected"), bool) else None
        state = row.get("status") if row.get("status") in _STATUS else "error"
        if not _maybe_day(row.get("date")):
            state = "error"
        if state != "ok":
            selected = None
        labels = {"stale": "数据未对齐", "insufficient": "历史不足", "error": "分析不可用"}
        verdict = labels.get(state, "进入观察池" if selected is True else "未入选")
        return {"code": row["code"], "name": _label(row.get("name")),
                "date": _maybe_day(row.get("date")), "analysis_date": snapshot_day,
                "selected": selected, "verdict": verdict,
                "reason": ("本地策略判定已入选；详细规则仅在本机查看" if selected is True and state == "ok"
                           else "本地策略判定未入选；详细规则仅在本机查看" if selected is False and state == "ok"
                           else "本地分析的数据状态需在桌面核对，未提供有效入选判断"),
                "status": state, "close": _number(row.get("close")),
                "change_pct": _number(row.get("change_pct"))}

    def _analysis_rows(self):
        snapshots, warnings = self._load_analysis()
        warnings = list(warnings)
        records = {}
        for snapshot in snapshots:
            for row in snapshot["rows"]:
                if not isinstance(row, dict) or not isinstance(row.get("code"), str) or not _CODE.fullmatch(row["code"]):
                    continue
                row_day = _maybe_day(row.get("date"))
                if row_day and row_day > self.as_of:
                    warnings.append("分析记录日期晚于任务日期，已排除")
                    continue
                current = self._public_analysis(row, snapshot["date"])
                code = row["code"]
                source = {"path": snapshot["path"], "code": code, "date": row_day or snapshot["date"]}
                if code in records:
                    previous, _, version = records[code]
                    if previous != current or version != snapshot["private_version"]:
                        warnings.append("同日分析缓存存在不同判定或参数版本，采用最近保存结果；请在桌面重新分析确认")
                else:
                    records[code] = current, source, snapshot["private_version"]
        if snapshots:
            warnings.append("当前缓存无可公开的策略版本标识；规则与完整版本仅保存在本机")
        return records, warnings

    def _get_analysis(self, code):
        records, warnings = self._analysis_rows()
        if code not in records:
            return self._result("missing", {"code": code, "message": "没有任务交易日对应的已保存分析，请先在桌面分析"}, warnings=warnings)
        row, source, _ = records[code]
        if row["date"] and row["date"] < self.as_of:
            warnings.append("该记录的行情早于任务日期，不可视为当日行情判断")
        if row["status"] != "ok":
            warnings.append("该分析存在数据质量或历史长度限制，请勿直接作为有效选股信号")
        return self._result(data=row, data_date=row["date"], sources=[source], warnings=warnings)

    def _get_candidates(self, limit=20):
        records, warnings = self._analysis_rows()
        snapshots = self._load_analysis()[0]
        if not snapshots:
            return self._result("missing", {"stocks": [], "count": 0, "returned": 0,
                                          "message": "没有任务交易日对应的已保存候选结果，请先在桌面分析"}, warnings=warnings)
        scopes = sorted({"all" if snapshot["path"].endswith("analysis_all.json") else "watchlist"
                         for snapshot in snapshots})
        if "all" not in scopes:
            warnings.append("当前只有自选分析缓存，候选列表未覆盖全市场")
        candidates = [(row, source) for row, source, _ in records.values()
                      if row["selected"] is True and row["status"] == "ok" and row["date"] == self.as_of]
        candidates.sort(key=lambda pair: pair[0]["code"])
        visible = candidates[:limit]
        if len(candidates) > limit:
            warnings.append("候选结果已按数量上限截断，可在桌面查看全部")
        return self._result(data={"stocks": [row for row, _ in visible], "count": len(candidates),
                                  "returned": len(visible), "analysis_date": self.as_of,
                                  "analysis_scopes": scopes},
                            data_date=self.as_of, sources=[source for _, source in visible], warnings=warnings)
