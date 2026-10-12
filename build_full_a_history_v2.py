#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
全A历史数据构建器 V2.0
用途：
1) 获取沪深A股名单
2) 排除 ST/*ST/退市整理、北交所
3) 逐股保存原始历史日线，支持断点续传
4) 失败重试并记录
5) 默认只维护逐股CSV持久化历史库；仅显式 --merge 时才生成总池

依赖：pandas, numpy, akshare
数据：AKShare stock_info_a_code_name + stock_zh_a_daily，非复权
"""
from __future__ import annotations

import argparse
import time
from datetime import date
from pathlib import Path

import pandas as pd
import akshare as ak


def norm_code(x):
    s = str(x).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s.zfill(6)


def is_sh_sz(code):
    code = norm_code(code)
    # 沪深A常见代码段；明确排除北交所 4/8/92 等。
    return code.startswith(("000", "001", "002", "003", "300", "301", "600", "601", "603", "605", "688", "689"))


def market_of(code):
    code = norm_code(code)
    if code.startswith(("688", "689")):
        return "科创板"
    if code.startswith(("300", "301")):
        return "创业板"
    return "主板"


def symbol_of(code):
    code = norm_code(code)
    return ("sh" if code.startswith(("5", "6", "9")) else "sz") + code


def excluded_name(name):
    n = str(name).upper().replace(" ", "")
    return ("ST" in n) or ("退" in n)


def get_stock_list():
    x = ak.stock_info_a_code_name().copy()
    cols = {str(c).lower(): c for c in x.columns}
    code_col = cols.get("code") or cols.get("代码")
    name_col = cols.get("name") or cols.get("名称")
    if code_col is None or name_col is None:
        # AKShare常见返回就是 code/name；若接口字段改变，明确失败而不是猜。
        if len(x.columns) >= 2:
            code_col, name_col = x.columns[:2]
        else:
            raise RuntimeError(f"无法识别股票名单字段: {list(x.columns)}")

    x = x[[code_col, name_col]].rename(columns={code_col: "code", name_col: "name"})
    x["code"] = x["code"].map(norm_code)
    x["name"] = x["name"].astype(str).str.strip()
    x = x[x["code"].map(is_sh_sz)]
    x = x[~x["name"].map(excluded_name)]
    x = x.drop_duplicates("code").sort_values("code").reset_index(drop=True)
    x["market"] = x["code"].map(market_of)
    return x


def normalize_daily(raw, code, name):
    d = raw.copy()
    d.columns = [str(c).lower() for c in d.columns]
    need = ["date", "open", "high", "low", "close", "volume"]
    miss = [c for c in need if c not in d.columns]
    if miss:
        raise ValueError(f"历史行情缺字段 {miss}; 实际字段={list(d.columns)}")

    d["date"] = pd.to_datetime(d["date"], errors="coerce")
    for c in ["open", "high", "low", "close", "volume", "amount", "outstanding_share", "turnover"]:
        if c in d.columns:
            d[c] = pd.to_numeric(d[c], errors="coerce")

    d = d.dropna(subset=need).sort_values("date").drop_duplicates("date", keep="last")
    d["code"] = norm_code(code)
    d["name"] = str(name)
    d["market"] = market_of(code)
    d["is_st"] = False

    # 实际交易日序号；selector最终仍以实际bar数判断上市>=120日。
    d["listed_days"] = range(1, len(d) + 1)

    prev = d["close"].shift(1)
    pct = d["close"] / prev - 1
    threshold = 0.195 if market_of(code) in ("创业板", "科创板") else 0.095
    d["limit_up"] = (pct >= threshold).fillna(False)

    preferred = [
        "code", "name", "market", "date", "open", "high", "low", "close",
        "volume", "amount", "outstanding_share", "turnover",
        "is_st", "listed_days", "limit_up"
    ]
    return d[[c for c in preferred if c in d.columns]]


def download_one(code, name, start_date, end_date, retries):
    err = None
    for attempt in range(1, retries + 1):
        try:
            raw = ak.stock_zh_a_daily(
                symbol=symbol_of(code),
                start_date=start_date,
                end_date=end_date,
                adjust=""
            )
            if raw is None or raw.empty:
                raise RuntimeError("接口返回空数据")
            return normalize_daily(raw, code, name), None
        except Exception as e:
            err = e
            if attempt < retries:
                time.sleep(1.5 * attempt)
    return None, str(err)


def safe_save_csv(df, path):
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_csv(tmp, index=False, encoding="utf-8-sig")
    tmp.replace(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=r"data\full_a")
    ap.add_argument("--processed", default=r"data\processed\a_full_pool.csv")
    ap.add_argument("--start", default="19900101",
                    help="历史开始日期 YYYYMMDD，默认尽可能覆盖A股历史")
    ap.add_argument("--end", default=date.today().strftime("%Y%m%d"))
    ap.add_argument("--sleep", type=float, default=0.35)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--force", action="store_true",
                    help="忽略已有单股CSV，强制重新下载")
    ap.add_argument("--merge", action="store_true",
                    help="显式生成总池 data/processed/a_full_pool.csv；默认不合并，推荐逐股生产架构")
    args = ap.parse_args()

    root = Path(args.root)
    stocks_dir = root / "stocks"
    stocks_dir.mkdir(parents=True, exist_ok=True)
    processed = Path(args.processed)
    processed.parent.mkdir(parents=True, exist_ok=True)

    print("========== 全A历史数据构建器 V2.0 ==========")
    print("正在获取沪深A股名单...")
    universe = get_stock_list()
    safe_save_csv(universe, root / "stock_list.csv")

    print("股票数:", len(universe))
    print("历史区间:", args.start, "->", args.end)
    print("单股目录:", stocks_dir.resolve())
    print("断点续传: 开启（已有有效CSV默认跳过）")

    status = []
    success = 0
    skipped = 0
    failed = 0

    for idx, r in universe.iterrows():
        code, name = r["code"], r["name"]
        path = stocks_dir / f"{code}.csv"

        if path.exists() and path.stat().st_size > 100 and not args.force:
            try:
                old = pd.read_csv(path, dtype={"code": str})
                old_dates = pd.to_datetime(old["date"], format="mixed", errors="coerce")
                valid = int(old_dates.notna().sum())
                if valid >= 1:
                    skipped += 1
                    status.append({
                        "code": code, "name": name, "status": "SKIPPED_EXISTING",
                        "rows": valid, "last_date": str(old_dates.max().date()), "error": ""
                    })
                    print(f"[{idx+1}/{len(universe)}] {code} {name}: 已存在 {valid} 行，跳过")
                    continue
            except Exception:
                pass

        d, err = download_one(code, name, args.start, args.end, args.retries)
        if d is None:
            failed += 1
            status.append({
                "code": code, "name": name, "status": "FAILED",
                "rows": 0, "last_date": "", "error": err
            })
            print(f"[{idx+1}/{len(universe)}] {code} {name}: 失败 {err}")
        else:
            safe_save_csv(d, path)
            success += 1
            status.append({
                "code": code, "name": name, "status": "DOWNLOADED",
                "rows": len(d), "last_date": str(d["date"].max().date()), "error": ""
            })
            print(f"[{idx+1}/{len(universe)}] {code} {name}: {len(d)} 行 -> {d['date'].max().date()}")

        # 每只完成后都刷新状态，异常中断也能看到进度。
        safe_save_csv(pd.DataFrame(status), root / "download_status.csv")
        time.sleep(args.sleep)

    stat = pd.DataFrame(status)
    safe_save_csv(stat, root / "download_status.csv")
    if len(stat):
        safe_save_csv(stat[stat["status"] == "FAILED"], root / "failed.csv")

    print("\n========== 下载阶段完成 ==========")
    print("本次下载成功:", success)
    print("已有文件跳过:", skipped)
    print("失败:", failed)

    if not args.merge:
        print("默认逐股持久化模式：不生成总池。")
        print("生产行情库:", stocks_dir.resolve())
        print("如确需兼容旧总CSV，请显式增加 --merge。")
        return

    print("\n========== 合并全A历史池 ==========")
    frames = []
    merge_failed = []
    files = sorted(stocks_dir.glob("*.csv"))

    for i, p in enumerate(files, 1):
        try:
            d = pd.read_csv(p, dtype={"code": str})
            d["code"] = d["code"].astype(str).str.zfill(6)
            d["date"] = pd.to_datetime(d["date"], format="mixed", errors="coerce")
            d = d.dropna(subset=["date"])
            if len(d):
                frames.append(d)
        except Exception as e:
            merge_failed.append((p.stem, str(e)))

        if i % 500 == 0:
            print(f"已读取 {i}/{len(files)} 个单股文件")

    if not frames:
        raise SystemExit("没有可合并的单股历史数据。")

    pool = pd.concat(frames, ignore_index=True, sort=False)
    pool["code"] = pool["code"].astype(str).str.zfill(6)
    pool["date"] = pd.to_datetime(pool["date"], format="mixed", errors="coerce")
    pool = pool.dropna(subset=["date"])
    pool = pool.sort_values(["code", "date"]).drop_duplicates(["code", "date"], keep="last")

    safe_save_csv(pool, processed)
    if merge_failed:
        pd.DataFrame(merge_failed, columns=["code", "error"]).to_csv(
            root / "merge_failed.csv", index=False, encoding="utf-8-sig"
        )

    latest = pool["date"].max()
    last_by_code = pool.groupby("code")["date"].max()
    fresh = int((last_by_code == latest).sum())
    enough120 = int((pool.groupby("code").size() >= 120).sum())

    print("\n========== 全A历史池完成 ==========")
    print("成功合并股票数:", pool["code"].nunique())
    print("总记录数:", f"{len(pool):,}")
    print("至少120交易日股票数:", enough120)
    print("数据最新日:", latest.date())
    print("最新日有行情股票数:", fresh)
    print("总池:", processed.resolve())
    print("状态:", (root / "download_status.csv").resolve())
    print("失败:", (root / "failed.csv").resolve())


if __name__ == "__main__":
    main()
