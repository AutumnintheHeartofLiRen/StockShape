"""StockShape desktop entry point; data and research scripts stay external."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import traceback


def project_root():
    explicit = os.environ.get("STOCKSHAPE_ROOT")
    if explicit:
        return Path(explicit).resolve()
    origin = Path(sys.executable) if getattr(sys, "frozen", False) else Path(__file__)
    for folder in origin.resolve().parents:
        if (folder / "requirements-desktop.txt").is_file() and (folder / "desktop").is_dir():
            return folder
    return origin.resolve().parent


if __name__ == "__main__":
    root = project_root()
    os.environ["STOCKSHAPE_ROOT"] = str(root)
    try:
        from desktop.main import main
        raise SystemExit(main(root))
    except Exception:
        directory = root / "desktop_data"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "startup_error.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise
