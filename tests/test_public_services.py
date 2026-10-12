"""Offline framework tests using synthetic data and a synthetic provider."""

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import pandas as pd

from desktop.services import AnalysisCancelled, MIN_COVERAGE, Store, clean_json
from desktop.strategy import StrategyNotConfigured, load_strategy, save_rsi6_threshold


SYNTHETIC_PROVIDER = '''
from desktop.services import _cancelled


class Engine:
    def __init__(self, store):
        self.store = store

    def verify_sources(self):
        return {"synthetic_provider": "ok"}

    def analyze_codes(self, codes, progress=None, cancel=None):
        rows = []
        values = list(codes)
        if progress:
            progress(0, len(values), "synthetic start")
        for index, code in enumerate(values, 1):
            _cancelled(cancel)
            bars = self.store.load_bars(code)
            rows.append({"code": str(code), "name": "Synthetic", "date": "2026-01-02",
                         "close": float(bars.iloc[-1]["close"]), "change_pct": 0.0,
                         "selected": False, "verdict": "合成结果", "reason": "接口测试",
                         "metrics": {}, "status": "ok"})
            if progress:
                progress(index, len(values), "synthetic row")
        self.store.last_summary = {"date": "2026-01-02", "total": len(rows),
                                   "selected": 0, "failed": 0,
                                   "strategy": self.store.strategy()}
        return rows


def create_engine(store):
    return Engine(store)
'''


class PublicServicesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.store.stocks_dir.mkdir(parents=True)
        (self.store.stocks_dir / "000001.csv").write_text(
            "code,name,market,date,open,high,low,close,volume\n"
            "000001,Synthetic,Test,2026-01-02,10,11,9,10.5,100\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def configure(self, provider=SYNTHETIC_PROVIDER):
        private = self.root / "private"
        private.mkdir(exist_ok=True)
        (private / "strategy_engine.py").write_text(provider, encoding="utf-8")
        self.write_config()

    def write_config(self, **values):
        config = {"version": 1, "rsi6_threshold": 50, "previous_rsi6_threshold": 60,
                  "baseline_rsi6_threshold": 60, "changes": []}
        config.update(values)
        (self.root / "strategy_config.json").write_text(json.dumps(config), encoding="utf-8")

    def test_missing_provider_keeps_data_watchlist_and_bars_available(self):
        availability = self.store.analysis_availability()
        self.assertFalse(availability["available"])
        self.assertTrue(availability["reason"])
        self.assertEqual(self.store.load_universe()[0]["name"], "Synthetic")
        self.assertEqual(len(self.store.load_bars("000001")), 1)
        self.store.add_watch("1")
        self.assertEqual(Store(self.root).watchlist(), ["000001"])
        with self.assertRaises(StrategyNotConfigured):
            self.store.analyze_codes(["000001"])
        with self.assertRaises(StrategyNotConfigured):
            self.store.verify_frozen_sources()
        self.assertFalse((self.root / "strategy_config.json").exists())

    def test_missing_config_does_not_execute_provider_or_create_settings(self):
        private = self.root / "private"
        private.mkdir()
        marker = private / "executed.txt"
        (private / "strategy_engine.py").write_text(
            "from pathlib import Path\nPath(__file__).with_name('executed.txt').touch()\n", encoding="utf-8")
        self.assertFalse(self.store.analysis_availability()["available"])
        with self.assertRaises(StrategyNotConfigured):
            self.store.analyze_codes(["000001"])
        self.assertFalse(marker.exists())
        self.assertFalse((self.root / "strategy_config.json").exists())

    def test_invalid_config_disables_analysis_without_overwriting_it(self):
        self.configure()
        path = self.root / "strategy_config.json"
        path.write_text("broken", encoding="utf-8")
        self.assertFalse(self.store.analysis_availability()["available"])
        with self.assertRaises(StrategyNotConfigured):
            self.store.analyze_codes(["000001"])
        self.assertEqual(path.read_text(encoding="utf-8"), "broken")

    def test_provider_protocol_delegates_results_summary_and_progress(self):
        self.configure()
        self.assertTrue(self.store.analysis_availability()["available"])
        self.assertEqual(self.store.verify_frozen_sources(), {"synthetic_provider": "ok"})
        events = []
        self.store.add_watch("000001")
        rows = self.store.analyze_watchlist(progress=lambda *args: events.append(args))
        saved = json.loads((self.store.user_dir / "analysis_watchlist.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["rows"], rows)
        self.assertEqual(saved["summary"]["total"], 1)
        self.assertEqual(saved["summary"]["strategy"]["rsi6_threshold"], 50)
        self.assertEqual(events[0][:2], (0, 1))
        self.assertEqual(events[-1][:2], (1, 1))
        self.assertTrue((self.store.user_dir / "analysis_watchlist.csv").exists())

    def test_provider_is_recreated_after_settings_change(self):
        self.configure()
        self.store.analyze_codes(["000001"])
        self.assertEqual(self.store.last_summary["strategy"]["rsi6_threshold"], 50)
        self.store.set_rsi6_threshold(60)
        self.store.analyze_codes(["000001"])
        self.assertEqual(self.store.last_summary["strategy"]["rsi6_threshold"], 60)

    def test_invalid_provider_protocol_has_friendly_error(self):
        for provider in ["value = 1\n", "def create_engine(store): return object()\n"]:
            with self.subTest(provider=provider):
                self.configure(provider)
                with self.assertRaises(StrategyNotConfigured):
                    self.store.analyze_codes(["000001"])

    def test_cancelled_analysis_preserves_previous_result(self):
        self.configure()
        self.store.add_watch("000001")
        self.store.analyze_watchlist()
        path = self.store.user_dir / "analysis_watchlist.json"
        before = path.read_bytes()
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(AnalysisCancelled):
            self.store.analyze_watchlist(cancel=cancel)
        self.assertEqual(path.read_bytes(), before)

    def test_empty_market_preserves_previous_result(self):
        self.configure()
        self.store.analyze_all()
        path = self.store.user_dir / "analysis_all.json"
        before = path.read_bytes()
        (self.store.stocks_dir / "000001.csv").unlink()
        with self.assertRaises(ValueError):
            self.store.analyze_all()
        self.assertEqual(path.read_bytes(), before)

    def test_watchlist_rejects_missing_stock_and_invalid_path(self):
        for code in ["999999", "../000001", "000001.csv"]:
            with self.subTest(code=code), self.assertRaises(ValueError):
                self.store.add_watch(code)
        self.assertEqual(self.store.watchlist(), [])

    def test_universe_metadata_refresh_uses_updated_name(self):
        path = self.store.stocks_dir.parent / "daily_update_status.csv"
        path.write_text("code,name,new_last_date\n000001,First,2026-01-02\n", encoding="utf-8")
        self.assertEqual(self.store.load_universe()[0]["name"], "First")
        path.write_text("code,name,new_last_date\n000001,Second,2026-01-02\n", encoding="utf-8")
        self.assertEqual(self.store.load_universe()[0]["name"], "First")
        self.assertEqual(self.store.load_universe(refresh=True)[0]["name"], "Second")

    def test_bar_cleaning_reports_invalid_and_duplicate_rows(self):
        path = self.store.stocks_dir / "000001.csv"
        with path.open("a", encoding="utf-8") as output:
            output.write("000001,Synthetic,Test,2026-01-01,8,10,7,9,80\n")
            output.write("000001,Synthetic,Test,2026-01-02,10,11,9,10.6,110\n")
            output.write("000001,Synthetic,Test,2026-01-03,10,9,11,10,100\n")
        bars = self.store.load_bars("000001")
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars.iloc[-1]["close"], 10.6)
        self.assertEqual(bars.attrs["invalid_ohlcv_rows"], 1)
        self.assertEqual(bars.attrs["duplicate_dates"], 1)

    def test_market_status_is_independent_of_any_strategy_files(self):
        rows = [{"code": f"{index:06}", "new_last_date": "2026-01-02", "status": "updated", "error": ""}
                for index in range(1, 21)]
        rows[-1].update(new_last_date="2026-01-01", status="failed", error="offline")
        rows.append({"code": "sh000001", "new_last_date": "2026-01-01", "status": "updated", "error": ""})
        pd.DataFrame(rows).to_csv(self.store.stocks_dir.parent / "daily_update_status.csv", index=False)
        status = self.store.market_status()
        self.assertEqual(status["date"], "2026-01-02")
        self.assertEqual((status["total"], status["covered"], status["failed"]), (20, 19, 1))
        self.assertEqual(status["coverage"], MIN_COVERAGE)
        self.assertFalse((self.root / "private").exists())
        self.assertFalse((self.root / "strategy_config.json").exists())

    def test_strategy_missing_requires_explicit_configuration(self):
        with self.assertRaises(StrategyNotConfigured):
            load_strategy(self.root)
        with self.assertRaises(StrategyNotConfigured):
            save_rsi6_threshold(self.root, 50)
        self.assertFalse((self.root / "strategy_config.json").exists())

    def test_synthetic_strategy_persistence_and_baseline_compatibility(self):
        self.write_config()
        updated = save_rsi6_threshold(self.root, 60)
        self.assertEqual(updated["previous_rsi6_threshold"], 50)
        self.assertEqual(updated["baseline_rsi6_threshold"], 60)
        self.assertEqual(updated["changes"][-1]["from"], 50)
        self.assertEqual(updated["changes"][-1]["to"], 60)
        self.assertEqual(load_strategy(self.root), updated)
        self.assertEqual(save_rsi6_threshold(self.root, 60)["changes"], updated["changes"])
        legacy = {"rsi6_threshold": 50, "previous_rsi6_threshold": 60}
        (self.root / "strategy_config.json").write_text(json.dumps(legacy), encoding="utf-8")
        self.assertEqual(load_strategy(self.root)["baseline_rsi6_threshold"], 60)

    def test_invalid_synthetic_threshold_preserves_settings(self):
        self.write_config()
        path = self.root / "strategy_config.json"
        before = path.read_bytes()
        for value in [0, 101, float("nan"), float("inf"), True, None, "invalid"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                save_rsi6_threshold(self.root, value)
            self.assertEqual(path.read_bytes(), before)

    def test_failed_atomic_config_write_preserves_settings(self):
        self.write_config()
        path = self.root / "strategy_config.json"
        before = path.read_bytes()
        with patch("desktop.strategy.os.replace", side_effect=OSError("locked")), self.assertRaises(OSError):
            save_rsi6_threshold(self.root, 60)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_clean_json_replaces_non_finite_values(self):
        self.assertEqual(clean_json({"missing": float("nan"), "date": pd.Timestamp("2026-01-02")}),
                         {"missing": None, "date": "2026-01-02"})


if __name__ == "__main__":
    unittest.main()
