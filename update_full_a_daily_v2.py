#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""
全A逐股日线增量更新器 V2.0

生产数据源:
    <project>/data/full_a/stocks/<code>.csv

用途:
- 逐股读取本地最后交易日，只补缺失日线
- 保持非复权 adjust=""，与现有全A历史库口径一致
- 自动探测 AKShare stock_zh_a_daily 的 symbol 形式：
      6位代码 / sh|sz 前缀
- 原子写入；单股失败不会破坏原CSV
- 输出进度、ETA、更新状态和失败日志
- 不修改任何选股规则
"""

import argparse
import time
from pathlib import Path

import akshare as ak
import numpy as np
import pandas as pd


def fmt_time(sec):
    if sec is None or not np.isfinite(sec):
        return "--"
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def market_symbol(code):
    code = str(code).zfill(6)
    return ("sh" if code.startswith(("5", "6", "9")) else "sz") + code


def market_of(code):
    code = str(code).zfill(6)
    if code.startswith(("688", "689")):
        return "科创板"
    if code.startswith(("300", "301")):
        return "创业板"
    return "主板"


def excluded_name(name):
    n = str(name).upper().replace(" ", "")
    return ("ST" in n) or ("退" in n)


def progress(i, total, label, started, updated, failed, no_new):
    elapsed = time.time() - started
    rate = i / elapsed if elapsed > 0 else 0
    eta = (total - i) / rate if rate > 0 else None
    width = 26
    fill = int(width * i / total) if total else width
    bar = "█" * fill + "-" * (width - fill)
    pct = 100 * i / total if total else 100
    print(
        f"\r[{bar}] {pct:6.2f}% {i}/{total} "
        f"当前:{label[:18]:18s} 已用:{fmt_time(elapsed)} ETA:{fmt_time(eta)} "
        f"更新:{updated} 无新增:{no_new} 失败:{failed}",
        end="", flush=True
    )
    if i == total:
        print()


def normalize(raw, code, name):
    if raw is None or raw.empty:
        return pd.DataFrame()

    d = raw.copy()
    rename = {}
    for c in d.columns:
        lc = str(c).lower()
        if lc in ("date", "日期"):
            rename[c] = "date"
        elif lc in ("open", "开盘"):
            rename[c] = "open"
        elif lc in ("high", "最高"):
            rename[c] = "high"
        elif lc in ("low", "最低"):
            rename[c] = "low"
        elif lc in ("close", "收盘"):
            rename[c] = "close"
        elif lc in ("volume", "成交量"):
            rename[c] = "volume"
        elif lc in ("amount", "成交额"):
            rename[c] = "amount"
        elif lc in ("outstanding_share", "流通股本"):
            rename[c] = "outstanding_share"
        elif lc in ("turnover", "换手率"):
            rename[c] = "turnover"

    d = d.rename(columns=rename)
    need = ["date", "open", "high", "low", "close", "volume"]
    missing = [c for c in need if c not in d.columns]
    if missing:
        raise ValueError(f"返回字段缺失: {missing}; 实际字段: {list(d.columns)}")

    d["date"] = pd.to_datetime(d["date"], format="mixed", errors="coerce")
    for c in ["open", "high", "low", "close", "volume", "amount",
              "outstanding_share", "turnover"]:
        if c in d.columns:
            d[c] = pd.to_numeric(d[c], errors="coerce")

    d = (
        d.dropna(subset=need)
         .sort_values("date")
         .drop_duplicates("date", keep="last")
         .reset_index(drop=True)
    )
    d["code"] = str(code).zfill(6)
    d["name"] = str(name)
    d["market"] = market_of(code)
    d["is_st"] = bool(excluded_name(name))

    preferred = [
        "code", "name", "market", "date", "open", "high", "low", "close", "volume",
        "amount", "outstanding_share", "turnover", "is_st"
    ]
    cols = [c for c in preferred if c in d.columns]
    extra = [c for c in d.columns if c not in cols]
    return d[cols + extra]


def load_old(path):
    d = pd.read_csv(path, dtype={"code": str})
    if "date" not in d.columns:
        raise ValueError("本地CSV缺少 date 字段")
    d["date"] = pd.to_datetime(d["date"], format="mixed", errors="coerce")
    d = (
        d.dropna(subset=["date"])
         .sort_values("date")
         .drop_duplicates("date", keep="last")
         .reset_index(drop=True)
    )
    return d


def fetch(symbol, start_s, end_s):
    return ak.stock_zh_a_daily(
        symbol=symbol,
        start_date=start_s,
        end_date=end_s,
        adjust=""
    )


def probe_symbol_mode(files, end, max_probe=30):
    """
    不假定接口格式。
    找一只本地日期早于目标日期的股票，分别尝试:
      1) 6位代码
      2) sh/sz前缀
    谁能正常返回DataFrame就采用谁。

    若目标日期不是交易日/源尚未出数据，两种形式都可能返回空；
    此时“无异常返回”也视为接口可接受。
    """
    print("\n========== AKShare 接口预检 ==========")
    tested = 0
    errors = []

    for p in files:
        if tested >= max_probe:
            break
        try:
            old = load_old(p)
            if old.empty:
                continue
            last = pd.Timestamp(old["date"].max()).normalize()
            if last >= end:
                continue

            code = p.stem.zfill(6)
            start_s = (last + pd.Timedelta(days=1)).strftime("%Y%m%d")
            end_s = end.strftime("%Y%m%d")
            tested += 1

            candidates = [
                ("plain", code),
                ("prefixed", market_symbol(code)),
            ]
            accepted_empty = None

            for mode, symbol in candidates:
                try:
                    raw = fetch(symbol, start_s, end_s)
                    if raw is not None and not raw.empty:
                        print(f"[预检通过] {code}: symbol={symbol}，返回 {len(raw)} 行")
                        return mode
                    if accepted_empty is None:
                        accepted_empty = mode
                    print(f"[预检] {code}: symbol={symbol}，接口正常但暂无新K线")
                except Exception as e:
                    errors.append(f"{code} {symbol}: {type(e).__name__}: {e}")
                    print(f"[预检] {code}: symbol={symbol} 失败: {type(e).__name__}: {e}")

            if accepted_empty is not None:
                print(f"[预检通过] 两种形式暂无新K线；暂采用 {accepted_empty}")
                return accepted_empty

        except Exception as e:
            errors.append(f"{p.name}: {type(e).__name__}: {e}")

    if not tested:
        print("[预检] 所有本地股票已达到目标日期，无需探测。")
        return "plain"

    tail = "\n".join(errors[-6:])
    raise RuntimeError("AKShare symbol格式预检失败。\n" + tail)


def recompute_local_fields(merged, code):
    merged = (
        merged.sort_values("date")
              .drop_duplicates("date", keep="last")
              .reset_index(drop=True)
    )

    # 实际交易日序号
    merged["listed_days"] = np.arange(1, len(merged) + 1)

    # 保持现有历史库近似涨停口径
    close = pd.to_numeric(merged["close"], errors="coerce")
    pct = close / close.shift(1) - 1
    threshold = 0.195 if str(code).startswith(("300", "301", "688", "689")) else 0.095
    merged["limit_up"] = (pct >= threshold).fillna(False)
    return merged


def main():
    data_root = Path(__file__).resolve().parent / "data/full_a"
    ap = argparse.ArgumentParser(description="全A逐股历史库日线增量更新 V2.0")
    ap.add_argument("--stocks-dir", default=str(data_root / "stocks"))
    ap.add_argument("--status", default=str(data_root / "daily_update_status.csv"))
    ap.add_argument("--failed-log", default=str(data_root / "daily_update_failed.csv"))
    ap.add_argument("--end-date", default=None, help="YYYYMMDD；默认本机今天")
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--sleep", type=float, default=0.05)
    ap.add_argument(
        "--symbol-mode",
        choices=["auto", "plain", "prefixed"],
        default="auto",
        help="默认auto先预检；plain=6位代码；prefixed=sh/sz+代码"
    )
    args = ap.parse_args()

    stocks_dir = Path(args.stocks_dir)
    if not stocks_dir.exists():
        raise FileNotFoundError(f"股票目录不存在: {stocks_dir}")

    files = sorted(stocks_dir.glob("*.csv"))
    if not files:
        raise FileNotFoundError(f"目录内没有CSV: {stocks_dir}")

    end = (
        pd.Timestamp.today().normalize()
        if not args.end_date
        else pd.to_datetime(args.end_date, format="%Y%m%d").normalize()
    )
    end_s = end.strftime("%Y%m%d")

    # 每日刷新股票名称/ST状态。失败时仍可使用本地元数据继续更新。
    current_meta = {}
    try:
        u = ak.stock_info_a_code_name().copy()
        cols = {str(c).lower(): c for c in u.columns}
        code_col = cols.get("code") or cols.get("代码")
        name_col = cols.get("name") or cols.get("名称")
        if code_col is None or name_col is None:
            if len(u.columns) >= 2:
                code_col, name_col = u.columns[:2]
            else:
                raise RuntimeError("无法识别股票名单字段")
        for _, rr in u[[code_col, name_col]].iterrows():
            cc = str(rr[code_col]).strip()
            if cc.endswith(".0"):
                cc = cc[:-2]
            cc = cc.zfill(6)
            current_meta[cc] = str(rr[name_col]).strip()
        print(f"已刷新股票元数据: {len(current_meta)} 只")
    except Exception as e:
        print(f"[WARN] 股票名单刷新失败，将沿用本地name/is_st: {type(e).__name__}: {e}")

    print("========== 全A逐股日线增量更新 V2.0 ==========")
    print("目录:", stocks_dir)
    print("文件数:", len(files))
    print("目标日期:", end.date())
    print('价格口径: 非复权 adjust=""')
    print("说明: 只更新行情数据，不改变任何选股规则。")

    if args.symbol_mode == "auto":
        symbol_mode = probe_symbol_mode(files, end)
    else:
        symbol_mode = args.symbol_mode
    print("本次AKShare symbol模式:", symbol_mode)
    print()

    started = time.time()
    updated = failed = no_new = 0
    status = []

    for i, p in enumerate(files, 1):
        code = p.stem.zfill(6)
        name = ""
        old_last_value = ""
        try:
            old = load_old(p)
            if old.empty:
                raise ValueError("本地CSV为空")

            if "name" in old.columns and old["name"].notna().any():
                name = str(old["name"].dropna().iloc[-1])
            # 当前股票名单优先；用于处理更名/ST状态变化。
            if code in current_meta:
                name = current_meta[code]
            if "name" not in old.columns:
                old["name"] = name
            else:
                old["name"] = name
            old["market"] = market_of(code)
            old["is_st"] = bool(excluded_name(name))

            last = pd.Timestamp(old["date"].max()).normalize()
            old_last_value = last.date()

            if last >= end:
                no_new += 1
                status.append({
                    "code": code, "name": name,
                    "old_last_date": last.date(),
                    "new_last_date": last.date(),
                    "added_rows": 0,
                    "status": "already_current", "error": ""
                })
                progress(i, len(files), f"{code} {name}", started,
                         updated, failed, no_new)
                continue

            start_s = (last + pd.Timedelta(days=1)).strftime("%Y%m%d")
            symbol = code if symbol_mode == "plain" else market_symbol(code)

            raw = None
            last_err = None
            for attempt in range(1, args.retries + 1):
                try:
                    raw = fetch(symbol, start_s, end_s)
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    if attempt < args.retries:
                        time.sleep(1.0 * attempt)

            if last_err is not None:
                raise RuntimeError(
                    f"{type(last_err).__name__}: {last_err}"
                )

            if raw is None or raw.empty:
                no_new += 1
                status.append({
                    "code": code, "name": name,
                    "old_last_date": last.date(),
                    "new_last_date": last.date(),
                    "added_rows": 0,
                    "status": "no_new", "error": ""
                })
                progress(i, len(files), f"{code} {name}", started,
                         updated, failed, no_new)
                if args.sleep:
                    time.sleep(args.sleep)
                continue

            new = normalize(raw, code, name)
            if new.empty:
                raise ValueError("接口返回非空，但标准化后无有效K线")

            # 严格只接受本地最后日期之后、目标日期以内的数据
            new = new[(new["date"] > last) & (new["date"] <= end)].copy()
            if new.empty:
                no_new += 1
                status.append({
                    "code": code, "name": name,
                    "old_last_date": last.date(),
                    "new_last_date": last.date(),
                    "added_rows": 0,
                    "status": "no_new_after_filter", "error": ""
                })
                progress(i, len(files), f"{code} {name}", started,
                         updated, failed, no_new)
                continue

            before_len = len(old)
            merged = pd.concat([old, new], ignore_index=True, sort=False)
            merged["date"] = pd.to_datetime(
                merged["date"], format="mixed", errors="coerce"
            )
            merged = merged.dropna(subset=["date"])
            merged = recompute_local_fields(merged, code)
            merged["code"] = code
            merged["name"] = name
            merged["market"] = market_of(code)
            merged["is_st"] = bool(excluded_name(name))

            # 单股安全检查
            if len(merged) < before_len:
                raise RuntimeError("安全检查失败：合并后行数少于更新前")
            if pd.Timestamp(merged["date"].max()).normalize() < last:
                raise RuntimeError("安全检查失败：最新日期倒退")

            tmp = p.with_suffix(".csv.tmp")
            merged.to_csv(tmp, index=False, encoding="utf-8-sig")
            tmp.replace(p)

            new_last = pd.Timestamp(merged["date"].max()).normalize()
            added = len(merged) - before_len
            updated += 1
            status.append({
                "code": code, "name": name,
                "old_last_date": last.date(),
                "new_last_date": new_last.date(),
                "added_rows": added,
                "status": "updated", "error": ""
            })

        except Exception as e:
            failed += 1
            status.append({
                "code": code, "name": name,
                "old_last_date": old_last_value,
                "new_last_date": old_last_value,
                "added_rows": 0,
                "status": "failed",
                "error": f"{type(e).__name__}: {e}"
            })

        progress(i, len(files), f"{code} {name}", started,
                 updated, failed, no_new)
        if args.sleep:
            time.sleep(args.sleep)

    st = pd.DataFrame(status)

    status_path = Path(args.status)
    status_path.parent.mkdir(parents=True, exist_ok=True)
    st.to_csv(status_path, index=False, encoding="utf-8-sig")

    failed_path = Path(args.failed_log)
    failed_path.parent.mkdir(parents=True, exist_ok=True)
    st[st["status"] == "failed"].to_csv(
        failed_path, index=False, encoding="utf-8-sig"
    )

    dates = pd.to_datetime(st["new_last_date"], errors="coerce")
    market_latest = dates.max()
    latest_count = int((dates == market_latest).sum()) if pd.notna(market_latest) else 0
    failed_count = int((st["status"] == "failed").sum())

    print("\n========== 更新完成 ==========")
    print("更新成功股票数:", updated)
    print("无新增/已最新:", no_new)
    print("失败:", failed_count)
    print("库内最新日期:",
          market_latest.date() if pd.notna(market_latest) else "未知")
    print("达到库内最新日期股票数:", latest_count)
    print("状态文件:", status_path)
    print("失败日志:", failed_path)
    print("总耗时:", fmt_time(time.time() - started))

    if pd.notna(market_latest) and market_latest < end:
        print(f"\n[提示] 库内最新交易日仍是 {market_latest.date()}。")
        print("如果目标日不是交易日、尚未收盘或数据源尚未更新，这是正常的。")
        print("此时不要把目标日期本身当成已经完整更新。")
    elif pd.notna(market_latest):
        print(f"\n[OK] 库内已有股票达到 {market_latest.date()}。")
        print("请同时检查“达到库内最新日期股票数”和失败数，再决定是否运行选股器。")


if __name__ == "__main__":
    main()
