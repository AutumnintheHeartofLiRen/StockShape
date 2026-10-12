"""Public configuration tests use synthetic parameters, never owner settings."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from desktop.strategy import StrategyNotConfigured, load_strategy, save_rsi6_threshold


class StrategyConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "strategy_config.json"

    def install(self):
        self.path.write_text(json.dumps({"version": 1, "rsi6_threshold": 50,
            "baseline_rsi6_threshold": 60, "previous_rsi6_threshold": 60, "changes": []}), encoding="utf-8")

    def test_absent_config_does_not_guess_parameters_or_write_files(self):
        with self.assertRaises(StrategyNotConfigured):
            load_strategy(self.root)
        with self.assertRaises(StrategyNotConfigured):
            save_rsi6_threshold(self.root, 50)
        self.assertFalse(self.path.exists())

    def test_null_template_is_unconfigured(self):
        self.path.write_text('{"rsi6_threshold": null, "baseline_rsi6_threshold": null}', encoding="utf-8")
        with self.assertRaises(StrategyNotConfigured):
            load_strategy(self.root)

    def test_setting_restoring_and_history_preserve_original_baseline(self):
        self.install()
        saved = save_rsi6_threshold(self.root, 60)
        self.assertEqual(saved["previous_rsi6_threshold"], 50)
        self.assertEqual(saved["baseline_rsi6_threshold"], 60)
        self.assertEqual(saved["changes"][-1]["to"], 60)
        self.assertEqual(save_rsi6_threshold(self.root, 60)["changes"], saved["changes"])
        restored = save_rsi6_threshold(self.root, 50.5)
        self.assertEqual(restored["rsi6_threshold"], 50.5)
        self.assertEqual(restored["baseline_rsi6_threshold"], 60)

    def test_legacy_baseline_comes_from_local_settings(self):
        self.path.write_text('{"rsi6_threshold":50,"previous_rsi6_threshold":60}', encoding="utf-8")
        self.assertEqual(load_strategy(self.root)["baseline_rsi6_threshold"], 60)

    def test_invalid_values_do_not_replace_configuration(self):
        self.install()
        before = self.path.read_bytes()
        for value in [None, True, "bad", 0, 101, float("nan"), float("inf")]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                save_rsi6_threshold(self.root, value)
            self.assertEqual(self.path.read_bytes(), before)

    def test_atomic_failure_preserves_configuration(self):
        self.install()
        before = self.path.read_bytes()
        with patch("desktop.strategy.os.replace", side_effect=OSError("locked")), self.assertRaises(OSError):
            save_rsi6_threshold(self.root, 60)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_invalid_json_is_not_silently_replaced(self):
        self.path.write_text("invalid", encoding="utf-8")
        with self.assertRaises(ValueError):
            save_rsi6_threshold(self.root, 50)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "invalid")


if __name__ == "__main__":
    unittest.main()
