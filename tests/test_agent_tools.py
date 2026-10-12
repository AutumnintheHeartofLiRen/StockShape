"""Read-only evidence tools: date boundaries, bounded context and privacy."""

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from desktop.services import Store
from research_agent.tools import ResearchTools


class ResearchToolsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.stocks = self.root / "data" / "full_a" / "stocks"
        self.stocks.mkdir(parents=True)
        self.user = self.root / "desktop_data"
        self.user.mkdir()
        self.days = pd.bdate_range("2026-01-01", periods=65)
        self.as_of = self.days[49].strftime("%Y-%m-%d")
        for code, name in (("000001", "平安银行"), ("600000", "浦发银行")):
            prices = list(range(1, 66))
            pd.DataFrame({"date": self.days.strftime("%Y-%m-%d"), "code": code,
                          "name": name, "market": "A", "open": prices, "high": prices,
                          "low": prices, "close": prices, "volume": [100] * 65,
                          "private_unused_column": ["DO_NOT_SEND_RAW_CSV"] * 65}).to_csv(
                self.stocks / f"{code}.csv", index=False, encoding="utf-8-sig")
        self.status(self.as_of)
        (self.user / "watchlist.json").write_text(json.dumps({"codes": ["000001"]}), encoding="utf-8")
        self.store = Store(self.root)
        self.tools = ResearchTools(self.store, self.as_of)

    def status(self, day):
        pd.DataFrame({"code": ["000001", "600000"], "new_last_date": [day, day],
                      "status": ["ok", "ok"], "error": ["", ""]}).to_csv(
            self.stocks.parent / "daily_update_status.csv", index=False, encoding="utf-8-sig")

    def save_analysis(self, filename="analysis_all.json", day=None, rows=None, threshold=31):
        day = day or self.as_of
        rows = rows if rows is not None else [{
            "code": "000001", "name": "平安银行", "date": day, "close": 50,
            "change_pct": 2.04, "selected": True, "status": "ok", "verdict": "private_verdict",
            "reason": "PRIVATE_RSI_SYNTHETIC_RULE",
            "metrics": {"rsi6_threshold": threshold, "private_metric": "SECRET_METRIC"},
        }]
        (self.user / filename).write_text(json.dumps({"rows": rows, "summary": {
            "date": day, "strategy": {"rsi6_threshold": threshold, "private_secret": "SECRET_STRATEGY"},
            "rsi6_threshold": threshold, "frozen_source_hashes": {"secret.py": "SECRET_SOURCE_HASH"},
        }}, ensure_ascii=False), encoding="utf-8")

    def test_schemas_are_only_the_six_public_read_tools(self):
        schemas = self.tools.schemas()
        self.assertEqual({entry["function"]["name"] for entry in schemas}, {
            "search_stocks", "get_market_status", "get_watchlist", "get_bars", "get_analysis", "get_candidates"})
        for entry in schemas:
            self.assertFalse(entry["function"]["parameters"]["additionalProperties"])
        self.assertEqual(self.tools.toolschema("get_bars")["function"]["name"], "get_bars")
        self.assertIsNone(self.tools.toolschema("run_shell"))

    def test_search_and_watchlist_metadata(self):
        response = self.tools.execute("search_stocks", {"query": "银行", "limit": 1})
        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["data"]["count"], 2)
        self.assertEqual(len(response["data"]["stocks"]), 1)
        watch = self.tools.execute("get_watchlist", {})
        self.assertEqual(watch["data"]["stocks"][0]["code"], "000001")
        self.assertEqual(watch["as_of"], self.as_of)

    def test_default_day_uses_market_effective_day_and_is_pinned(self):
        tools = ResearchTools(self.store)
        self.assertEqual(tools.as_of, self.as_of)
        self.status(self.days[60].strftime("%Y-%m-%d"))
        self.assertEqual(tools.execute("get_market_status", {})["data_date"], self.as_of)
        self.assertEqual(tools.as_of, self.as_of)

    def test_invalid_dates_rejected_before_tool_calls(self):
        for invalid in ("2026-02-30", "2026-1-1", "2026-01-01T00:00:00", 20260101, "../../private"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                ResearchTools(self.store, invalid)

    def test_default_day_without_market_data_uses_shanghai_today(self):
        with patch.object(self.store, "market_status", return_value={"date": ""}):
            tools = ResearchTools(self.store)
        self.assertRegex(tools.as_of, r"^\d{4}-\d{2}-\d{2}$")

    def test_market_status_future_snapshot_is_not_historical_coverage(self):
        self.status(self.days[60].strftime("%Y-%m-%d"))
        tools = ResearchTools(Store(self.root), self.as_of)
        response = tools.execute("get_market_status", {})
        self.assertEqual(response["status"], "missing")
        self.assertIsNone(response["data_date"])
        self.assertTrue(any("未来" in text for text in response["warnings"]))

    def test_bars_cutoff_and_ma40_use_full_past_before_tail(self):
        response = self.tools.execute("get_bars", {"code": "000001", "limit": 2})
        self.assertEqual(response["status"], "ok")
        data = response["data"]
        self.assertEqual(data["total_bars"], 50)
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["last_close"], 50)
        self.assertEqual(data["ma40"], 30.5)  # Past closes 11 through 50.
        self.assertEqual(data["bars"][0]["ma40"], 29.5)
        self.assertAlmostEqual(data["change_pct"], (50 / 49 - 1) * 100)
        self.assertEqual(data["range"]["end"], self.as_of)
        self.assertTrue(all(row["date"] <= self.as_of for row in data["bars"]))
        self.assertNotIn("private_unused_column", json.dumps(response))
        self.assertNotIn("DO_NOT_SEND_RAW_CSV", json.dumps(response))

    def test_fewer_than_40_bars_yields_null_not_nan(self):
        tools = ResearchTools(self.store, self.days[38].strftime("%Y-%m-%d"))
        response = tools.execute("get_bars", {"code": "000001"})
        self.assertIsNone(response["data"]["ma40"])
        json.dumps(response, allow_nan=False)
        self.assertTrue(any("MA40" in text for text in response["warnings"]))

    def test_no_data_before_first_bar_and_missing_stock(self):
        early = ResearchTools(self.store, "2025-12-01")
        self.assertEqual(early.execute("get_bars", {"code": "000001"})["status"], "missing")
        self.assertEqual(self.tools.execute("get_bars", {"code": "123456"})["status"], "missing")

    def test_bad_ohlcv_and_duplicates_report_quality_warning(self):
        path = self.stocks / "000001.csv"
        frame = pd.read_csv(path)
        frame.loc[2, "close"] = -1
        frame = pd.concat([frame, frame.iloc[[4]]], ignore_index=True)
        frame.to_csv(path, index=False, encoding="utf-8-sig")
        response = self.tools.execute("get_bars", {"code": "000001"})
        self.assertEqual(response["status"], "ok")
        self.assertTrue(any("OHLCV" in text for text in response["warnings"]))
        self.assertTrue(any("重复" in text for text in response["warnings"]))
        self.assertTrue(all(row["close"] > 0 for row in response["data"]["bars"]))

    def test_argument_validation_blocks_path_traversal_and_overflow(self):
        bad_calls = [
            ("get_bars", {"code": "../private"}), ("get_bars", {"code": "000001/../../secret"}),
            ("get_bars", {"code": 1}), ("get_bars", {"code": "1"}),
            ("get_bars", {"code": "000001", "limit": True}),
            ("get_bars", {"code": "000001", "limit": 61}),
            ("get_bars", {"code": "000001", "limit": 0}),
            ("get_bars", {"code": "000001", "limit": "30"}),
            ("get_bars", {"code": "000001", "path": "secret"}),
            ("get_analysis", {"code": "000001", "as_of": "2030-01-01"}),
            ("search_stocks", {"query": " "}), ("search_stocks", {"query": "a" * 101}),
            ("search_stocks", {"query": "银行", "limit": 21}),
            ("get_candidates", {"limit": 31}), ("get_watchlist", {"write": True}),
            ("get_market_status", {"refresh": True}), ("run_shell", {}),
            ("get_bars", []), ("get_bars", {}),
        ]
        for name, arguments in bad_calls:
            with self.subTest(name=name, arguments=arguments):
                self.assertEqual(self.tools.execute(name, arguments)["status"], "error")

    def test_no_analysis_is_missing_never_runs_private_provider(self):
        with patch.object(self.store, "analyze_codes", side_effect=AssertionError("must not execute")), \
                patch.object(self.store, "strategy", side_effect=AssertionError("must not read")):
            self.assertEqual(self.tools.execute("get_analysis", {"code": "000001"})["status"], "missing")
            self.assertEqual(self.tools.execute("get_candidates", {})["status"], "missing")

    def test_analysis_only_returns_allowlisted_public_fields(self):
        self.save_analysis()
        response = self.tools.execute("get_analysis", {"code": "000001"})
        self.assertEqual(response["status"], "ok")
        self.assertTrue(response["data"]["selected"])
        self.assertEqual(response["data"]["verdict"], "进入观察池")
        self.assertIsNone(response["strategy_version"])
        rendered = json.dumps(response, ensure_ascii=False)
        for secret in ("PRIVATE_RSI", "SECRET_METRIC", "SECRET_SOURCE_HASH", "SECRET_STRATEGY",
                       "rsi6_threshold", "private_verdict", "metrics", str(self.root)):
            self.assertNotIn(secret, rendered)
        self.assertEqual(response["sources"][0]["path"], "desktop_data/analysis_all.json")
        self.assertTrue(any("版本" in text for text in response["warnings"]))

    def test_future_and_old_caches_never_stand_in_for_requested_day(self):
        for day in (self.days[48].strftime("%Y-%m-%d"), self.days[50].strftime("%Y-%m-%d")):
            with self.subTest(day=day):
                self.save_analysis(day=day)
                tools = ResearchTools(self.store, self.as_of)
                response = tools.execute("get_analysis", {"code": "000001"})
                self.assertEqual(response["status"], "missing")
                self.assertIsNone(response["data_date"])
                self.assertTrue(response["warnings"])

    def test_future_row_is_excluded_even_with_current_summary(self):
        self.save_analysis(rows=[{"code": "000001", "name": "平安银行", "date": "2030-01-01",
                                  "selected": True, "status": "ok"}])
        response = self.tools.execute("get_analysis", {"code": "000001"})
        self.assertEqual(response["status"], "missing")
        self.assertTrue(any("晚于" in text for text in response["warnings"]))

    def test_stale_or_invalid_row_never_becomes_candidate(self):
        yesterday = self.days[48].strftime("%Y-%m-%d")
        self.save_analysis(rows=[{"code": "000001", "date": yesterday, "selected": True, "status": "stale"},
                                 {"code": "600000", "date": "bad-date", "selected": True, "status": "ok"}])
        stale = self.tools.execute("get_analysis", {"code": "000001"})
        self.assertIsNone(stale["data"]["selected"])
        self.assertTrue(any("早于" in text for text in stale["warnings"]))
        self.assertEqual(self.tools.execute("get_analysis", {"code": "600000"})["data"]["status"], "error")
        self.assertEqual(self.tools.execute("get_candidates", {})["data"]["count"], 0)

    def test_strategy_conflict_is_flagged_and_latest_saved_result_wins(self):
        self.save_analysis("analysis_all.json", threshold=31)
        old_path = self.user / "analysis_all.json"
        os.utime(old_path, (1, 1))
        self.save_analysis("analysis_watchlist.json", threshold=58, rows=[{
            "code": "000001", "date": self.as_of, "selected": False, "status": "ok"}])
        response = self.tools.execute("get_analysis", {"code": "000001"})
        self.assertFalse(response["data"]["selected"])
        self.assertEqual(response["sources"][0]["path"], "desktop_data/analysis_watchlist.json")
        self.assertTrue(any("参数版本" in text for text in response["warnings"]))
        self.assertNotIn("rsi6", json.dumps(response))

    def test_candidates_are_bounded_and_deduplicated(self):
        self.save_analysis(rows=[{"code": f"{index:06d}", "date": self.as_of, "selected": True,
                                  "status": "ok", "metrics": {"secret": 1}} for index in range(35)])
        response = self.tools.execute("get_candidates", {"limit": 30})
        self.assertEqual(response["data"]["count"], 35)
        self.assertEqual(response["data"]["returned"], 30)
        self.assertEqual(len(response["sources"]), 30)
        self.assertTrue(any("截断" in text for text in response["warnings"]))

    def test_watchlist_cache_is_not_claimed_to_cover_all_stocks(self):
        self.save_analysis(filename="analysis_watchlist.json")
        response = self.tools.execute("get_candidates", {})
        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["data"]["analysis_scopes"], ["watchlist"])
        self.assertTrue(any("未覆盖全市场" in text for text in response["warnings"]))

    def test_read_errors_never_expose_absolute_path_or_file_contents(self):
        with patch.object(self.store, "load_universe", side_effect=ValueError(f"secret file {self.root}/private.py")):
            response = self.tools.execute("search_stocks", {"query": "银行"})
        self.assertEqual(response["status"], "error")
        self.assertNotIn(str(self.root), json.dumps(response))
        self.assertNotIn("private.py", json.dumps(response))

    def test_all_tools_leave_files_unchanged_and_do_not_read_private_package(self):
        self.save_analysis()
        private = self.root / "private"
        private.mkdir()
        (private / "strategy_engine.py").write_text("raise AssertionError('must not load')", encoding="utf-8")
        (self.root / "strategy_config.json").write_text("NOT VALID JSON: must not read", encoding="utf-8")

        def fingerprint():
            return {str(path.relative_to(self.root)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in self.root.rglob("*") if path.is_file()}

        before = fingerprint()
        for name, arguments in (("search_stocks", {"query": "银行"}), ("get_market_status", {}),
                                ("get_watchlist", {}), ("get_bars", {"code": "000001"}),
                                ("get_analysis", {"code": "000001"}), ("get_candidates", {})):
            with self.subTest(name=name):
                response = self.tools.execute(name, arguments)
                self.assertEqual(response["status"], "ok")
                json.dumps(response, allow_nan=False)
        self.assertEqual(before, fingerprint())


if __name__ == "__main__":
    unittest.main()
