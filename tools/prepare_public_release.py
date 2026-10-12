"""Export an explicit public-source allowlist without publishing local research.

Run --check for a read-only preflight. Exported files form a fresh repository
directory; this command does not initialize Git or upload anything. Keeping a
strategy package private protects its source from accidental publication. It is
not an authorization system and does not prevent recipients from sharing it.
"""
from __future__ import annotations

import argparse
import fnmatch
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_FILES = (
    "README.md", ".gitignore", ".env.example", "strategy_config.example.json",
    "requirements.txt", "requirements-desktop.txt", "start_desktop.py",
    "启动StockShape.vbs", "build_full_a_history_v2.py", "update_full_a_daily_v2.py",
    "desktop/__init__.py", "desktop/main.py", "desktop/chart.py",
    "desktop/services.py", "desktop/strategy.py", "desktop/worker.py",
    "desktop/agent_panel.py", "desktop/agent_worker.py",
    "research_agent/__init__.py", "research_agent/config.py",
    "research_agent/provider.py", "research_agent/tools.py", "research_agent/runner.py",
    "research_agent/discovery.py",
    "desktop/assets/stockshape.svg", "tools/build_desktop.py",
    "tools/prepare_public_release.py", "docs/MAINTENANCE.md",
    "docs/PUBLISHING.md", "docs/AI_ROADMAP.md", "docs/ARCHITECTURE.md", "docs/AI_AGENT.md",
    "tests/test_desktop_ui.py", "tests/test_desktop_chart.py",
    "tests/test_desktop_update_progress.py", "tests/test_public_services.py",
    "tests/test_public_release.py", "tests/test_strategy_config.py",
    "tests/test_agent_provider.py", "tests/test_agent_tools.py",
    "tests/test_agent_runner.py", "tests/test_agent_ui.py", "tests/test_agent_worker.py",
    "tests/test_agent_discovery.py",
)
PRIVATE_DIRECTORIES = frozenset({
    "private", "data", "desktop_data", "reference", "frozen_model_daily",
    "review_log", "build", "dist", ".venv", "__pycache__",
})
PRIVATE_FILES = frozenset({
    "strategy_config.json", "V15_history_shape_model.py",
    "V16_history_candidate_path_shape.py", "V9_shape_diagnostic.py",
    "rsw_manual_pattern_research_v2.py", "run_frozen_model_daily.py",
    "docs/MODEL.md", "tests/test_desktop_services.py", "tools/health_check.py", ".env",
})
# Files need not exist: probes also protect future files in local directories.
PRIVATE_PROBES = (
    *(f"{folder}/_publication_check_" for folder in sorted(PRIVATE_DIRECTORIES)),
    *sorted(PRIVATE_FILES), ".env.local", "desktop/__pycache__/module.pyc",
    "tests/fixtures/frozen_20260930.json", "tests/fixtures/_publication_check_",
)


class ReleaseError(ValueError):
    """The public export cannot be safely prepared."""


def _linked(path: Path) -> bool:
    return path.is_symlink() or getattr(os.path, "isjunction", lambda _: False)(path)


def _root_path(root) -> Path:
    path = Path(root).absolute()
    if _linked(path):
        raise ReleaseError(f"Project root cannot be a link: {path}")
    return path.resolve()


def _no_links(path: Path, root: Path):
    """Check each lexical ancestor, including broken symlinks."""
    if not path.is_relative_to(root):
        raise ReleaseError(f"Path is outside project: {path}")
    current = path
    while True:
        if _linked(current):
            raise ReleaseError(f"Links are not allowed in release paths: {current}")
        if current == root:
            break
        current = current.parent


def _public_name(name: str) -> str:
    relative = PurePosixPath(str(name).replace("\\", "/"))
    if relative.is_absolute() or not relative.parts or any(
        part in {".", ".."} or ":" in part for part in relative.parts
    ):
        raise ReleaseError(f"Invalid public file name: {name}")
    value = relative.as_posix()
    if (relative.parts[0] in PRIVATE_DIRECTORIES or value in PRIVATE_FILES
            or value.startswith("tests/fixtures/") or value.endswith(".pyc")
            or (relative.name.startswith(".env") and value != ".env.example")):
        raise ReleaseError(f"Private file cannot enter the public allowlist: {name}")
    return value


def validate_sources(root, public_files=PUBLIC_FILES) -> tuple[str, ...]:
    root = _root_path(root)
    names = tuple(_public_name(name) for name in public_files)
    if len(names) != len(set(names)):
        raise ReleaseError("Public allowlist contains duplicate files")
    missing = []
    for name in names:
        source = root / name
        _no_links(source, root)
        if not source.resolve().is_relative_to(root):
            raise ReleaseError(f"Public source resolves outside project: {source}")
        if not source.is_file():
            missing.append(name)
    if missing:
        raise ReleaseError("Public source files are missing: " + ", ".join(missing))
    return names


def _matches_rule(pattern: str, name: str) -> bool:
    """Conservative support for explicit root ignore rules in this project."""
    pattern = pattern.strip("/")
    name = name.rstrip("/")
    parts = name.split("/")
    if "/" not in pattern:
        return any(fnmatch.fnmatchcase(part, pattern) for part in parts)
    ancestors = ["/".join(parts[:index]) for index in range(1, len(parts) + 1)]
    return any(fnmatch.fnmatchcase(candidate, pattern) for candidate in ancestors)


def check_ignore_rules(root, public_files=PUBLIC_FILES) -> str:
    root = _root_path(root)
    path = root / ".gitignore"
    _no_links(path, root)
    if not path.is_file():
        raise ReleaseError("Missing .gitignore; private files need explicit ignore rules")
    rules = [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    # A private directory must stay wholly private, including future filenames.
    protected = PRIVATE_DIRECTORIES | {"tests/fixtures"}
    for rule in rules:
        if rule.startswith("!"):
            pattern = rule[1:].lstrip("/")
            if any(pattern == folder or pattern.startswith(folder + "/") for folder in protected):
                raise ReleaseError(f".gitignore re-includes a private directory: {rule}")

    def ignored(name):
        result = False
        for rule in rules:
            negative = rule.startswith("!")
            pattern = rule[1:] if negative else rule
            if _matches_rule(pattern, name):
                result = not negative
        return result

    missing = [name for name in PRIVATE_PROBES if not ignored(name)]
    hidden_public = [name for name in public_files if ignored(name)]
    if missing:
        raise ReleaseError(".gitignore does not protect local files: " + ", ".join(missing))
    if hidden_public:
        raise ReleaseError(".gitignore hides public source files: " + ", ".join(hidden_public))

    probes = [*PRIVATE_PROBES, *public_files]
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "--no-index", "--stdin"],
            input="\n".join(probes) + "\n", text=True, encoding="utf-8",
            capture_output=True, timeout=10,
        )
    except FileNotFoundError:
        return "explicit .gitignore rules (Git unavailable)"
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseError(f"Cannot verify Git ignore rules: {exc}") from exc
    if result.returncode not in (0, 1):
        if not (root / ".git").exists():
            return "explicit .gitignore rules (directory is not a Git working tree)"
        raise ReleaseError("git check-ignore failed: " + result.stderr.strip())
    actual = set(result.stdout.splitlines())
    missing = [name for name in PRIVATE_PROBES if name not in actual]
    hidden_public = [name for name in public_files if name in actual]
    if missing or hidden_public:
        raise ReleaseError(f"Git ignore mismatch; unprotected={missing}; hidden public={hidden_public}")
    return "explicit .gitignore rules and git check-ignore --no-index"


def check_release(root=ROOT, public_files=PUBLIC_FILES) -> dict:
    root = _root_path(root)
    names = validate_sources(root, public_files)
    method = check_ignore_rules(root, names)
    return {"root": str(root), "files": len(names), "ignore_check": method}


def export_release(root=ROOT, output=None, public_files=PUBLIC_FILES) -> dict:
    root = _root_path(root)
    public_files = tuple(public_files)
    summary = check_release(root, public_files)
    destination = Path(output) if output is not None else root / "build/public-release"
    if not destination.is_absolute():
        destination = root / destination
    _no_links(destination, root)
    resolved = destination.resolve()
    build = root / "build"
    if resolved == build or not resolved.is_relative_to(build):
        raise ReleaseError(f"Export output must be a child directory inside {build}")
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ReleaseError(f"Export output is not empty; choose a new directory: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    for name in public_files:
        source, target = root / name, destination / name
        _no_links(source, root)
        _no_links(target, root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return {**summary, "output": str(resolved)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="Read-only validation; write no files")
    group.add_argument("--output", type=Path, help="Empty output directory under project/build")
    args = parser.parse_args(argv)
    try:
        summary = check_release() if args.check else export_release(output=args.output)
    except ReleaseError as exc:
        print(f"FAIL: {exc}")
        return 1
    print(f"PASS: {summary['files']} explicit public files; {summary['ignore_check']}")
    if "output" in summary:
        print(f"Public source directory: {summary['output']}")
        print("No Git repository was initialized and nothing was uploaded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
