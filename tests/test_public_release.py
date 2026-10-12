"""Release-boundary checks use synthetic files and never publish production data."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from tools.prepare_public_release import (
    PRIVATE_DIRECTORIES, PRIVATE_FILES, ReleaseError,
    check_release, export_release, validate_sources,
)


class PublicReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.public_files = ("README.md", ".gitignore", ".env.example", "desktop/main.py")
        for name in self.public_files:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"public fixture: {name}\n", encoding="utf-8")
        self.rules = [*(folder + "/" for folder in sorted(PRIVATE_DIRECTORIES)),
                      *sorted(PRIVATE_FILES), "tests/fixtures/", "*.pyc", ".env.*", "!.env.example"]
        self.write_ignore()
        self.no_git = mock.patch("tools.prepare_public_release.subprocess.run", side_effect=FileNotFoundError())
        self.no_git.start()
        self.addCleanup(self.no_git.stop)

    def write_ignore(self):
        (self.root / ".gitignore").write_text("\n".join(self.rules) + "\n", encoding="utf-8")

    def test_check_is_read_only_and_explicit_when_git_unavailable(self):
        before = {str(path.relative_to(self.root)): path.read_bytes()
                  for path in self.root.rglob("*") if path.is_file()}
        summary = check_release(self.root, self.public_files)
        after = {str(path.relative_to(self.root)): path.read_bytes()
                 for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(summary["files"], 4)
        self.assertIn("Git unavailable", summary["ignore_check"])
        self.assertFalse((self.root / "build").exists())
        self.assertFalse((self.root / ".git").exists())

    def test_export_copies_allowlist_only_and_refuses_nonempty_output(self):
        (self.root / "private").mkdir()
        (self.root / "private/strategy_engine.py").write_text("private fixture\n", encoding="utf-8")
        (self.root / "strategy_config.json").write_text("private settings\n", encoding="utf-8")
        (self.root / "unlisted.txt").write_text("unlisted\n", encoding="utf-8")
        summary = export_release(self.root, public_files=self.public_files)
        output = Path(summary["output"])
        actual = {path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file()}
        self.assertEqual(actual, set(self.public_files))
        self.assertEqual((output / "desktop/main.py").read_bytes(), (self.root / "desktop/main.py").read_bytes())
        self.assertFalse((output / ".git").exists())
        with self.assertRaisesRegex(ReleaseError, "not empty"):
            export_release(self.root, public_files=self.public_files)
        next_output = export_release(self.root, "build/public-next", self.public_files)
        self.assertTrue(Path(next_output["output"]).is_dir())

    def test_missing_public_source_does_not_create_output(self):
        (self.root / "README.md").unlink()
        with self.assertRaisesRegex(ReleaseError, "missing"):
            export_release(self.root, public_files=self.public_files)
        self.assertFalse((self.root / "build").exists())

    def test_rejects_paths_outside_build_without_writing(self):
        for output in [self.root, self.root / "build", self.root.parent / "outside",
                       "data/export", "build/../../outside"]:
            with self.subTest(output=output), self.assertRaises(ReleaseError):
                export_release(self.root, output, self.public_files)
        self.assertFalse((self.root / "data").exists())

    def test_rejects_private_or_traversal_allowlist_names(self):
        for name in ["../README.md", "private/strategy_engine.py", "strategy_config.json",
                     "docs/MODEL.md", "tests/fixtures/anything.json", ".env.local", "desktop/module.pyc"]:
            with self.subTest(name=name), self.assertRaises(ReleaseError):
                validate_sources(self.root, [name])

    def test_requires_local_parameter_and_future_private_files_ignored(self):
        for missing in ["private/", "strategy_config.json", "tests/fixtures/", ".env.*"]:
            original = list(self.rules)
            self.rules.remove(missing)
            self.write_ignore()
            with self.subTest(missing=missing), self.assertRaisesRegex(ReleaseError, "does not protect"):
                check_release(self.root, self.public_files)
            self.rules = original
        self.write_ignore()

    def test_rejects_private_negation_and_hidden_public_sources(self):
        self.rules.append("!private/accidental.py")
        self.write_ignore()
        with self.assertRaisesRegex(ReleaseError, "re-includes"):
            check_release(self.root, self.public_files)
        self.rules[-1] = "desktop/"
        self.write_ignore()
        with self.assertRaisesRegex(ReleaseError, "hides public"):
            check_release(self.root, self.public_files)

    def test_git_working_tree_crosschecks_private_and_public_paths(self):
        from tools.prepare_public_release import PRIVATE_PROBES
        response = subprocess.CompletedProcess([], 0, "\n".join(PRIVATE_PROBES) + "\n", "")
        with mock.patch("tools.prepare_public_release.subprocess.run", return_value=response) as command:
            summary = check_release(self.root, self.public_files)
        self.assertIn("git check-ignore", summary["ignore_check"])
        self.assertIn("--no-index", command.call_args.args[0])
        response.stdout += "README.md\n"
        with mock.patch("tools.prepare_public_release.subprocess.run", return_value=response):
            with self.assertRaisesRegex(ReleaseError, "Git ignore mismatch"):
                check_release(self.root, self.public_files)

    def test_git_nonrepository_falls_back_without_initializing_git(self):
        response = subprocess.CompletedProcess([], 128, "", "fatal: not a git repository")
        with mock.patch("tools.prepare_public_release.subprocess.run", return_value=response):
            summary = check_release(self.root, self.public_files)
        self.assertIn("not a Git working tree", summary["ignore_check"])
        self.assertFalse((self.root / ".git").exists())

    def test_rejects_source_and_output_symlinks(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            external = Path(elsewhere)
            (external / "leak.txt").write_text("private fixture", encoding="utf-8")
            link = self.root / "linked"
            try:
                link.symlink_to(external, target_is_directory=True)
            except OSError:
                self.skipTest("Creating symlinks is unavailable on this Windows account")
            with self.assertRaisesRegex(ReleaseError, "Links"):
                validate_sources(self.root, ["linked/leak.txt"])
            (self.root / "build").mkdir()
            (self.root / "build/linked").symlink_to(external, target_is_directory=True)
            with self.assertRaisesRegex(ReleaseError, "Links"):
                export_release(self.root, "build/linked/release", self.public_files)
            self.assertFalse((external / "release").exists())

    def test_rejects_linked_source_without_requiring_windows_link_privilege(self):
        from tools.prepare_public_release import _linked
        source = self.root / "README.md"
        with mock.patch("tools.prepare_public_release._linked",
                        side_effect=lambda path: path == source or _linked(path)):
            with self.assertRaisesRegex(ReleaseError, "Links"):
                export_release(self.root, public_files=self.public_files)
        self.assertFalse((self.root / "build").exists())

    @unittest.skipUnless(os.name == "nt", "Windows junction check")
    def test_rejects_junction_build_directory(self):
        build = self.root / "build"
        actual_linked = __import__("tools.prepare_public_release", fromlist=["_linked"])._linked
        with mock.patch("tools.prepare_public_release._linked",
                        side_effect=lambda path: path == build or actual_linked(path)):
            with self.assertRaisesRegex(ReleaseError, "Links"):
                export_release(self.root, "build/release", self.public_files)
        self.assertFalse(build.exists())


if __name__ == "__main__":
    unittest.main()
