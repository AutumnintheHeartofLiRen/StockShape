"""Build the desktop shell; market data and Python workers remain in the project."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def check_output_path(path):
    resolved = path.resolve()
    if not resolved.is_relative_to(ROOT) or resolved == ROOT:
        raise ValueError(f"Build output must stay inside project: {path}")
    current = path
    while current != ROOT:
        if current.is_symlink() or os.path.isjunction(current):
            raise ValueError(f"Build output cannot contain a link: {current}")
        current = current.parent


def main():
    if not (ROOT / ".venv/Scripts/python.exe").is_file():
        raise SystemExit("Missing project .venv. Install requirements-desktop.txt first.")
    work = ROOT / "build/desktop"
    for path in [work, ROOT / "dist/StockShape"]:
        check_output_path(path)
    old_icu = ROOT / "dist/StockShape/_internal/icuuc.dll"
    if old_icu.exists():
        check_output_path(old_icu)
        old_icu.chmod(old_icu.stat().st_mode | 0o200)
    work.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QGuiApplication, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer
    app = QGuiApplication.instance() or QGuiApplication([])
    image = QImage(128, 128, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    QSvgRenderer(str(ROOT / "desktop/assets/stockshape.svg")).render(painter, QRectF(0, 0, 128, 128))
    painter.end()
    icon = work / "StockShape.ico"
    if not image.save(str(icon)):
        raise SystemExit("Could not generate application icon.")
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
               "--windowed", "--onedir", "--name", "StockShape",
               "--distpath", str(ROOT / "dist"), "--workpath", str(work / "work"),
               "--specpath", str(work), "--icon", str(icon),
               "--add-data", f"{ROOT / 'desktop/assets'};desktop/assets"]
    # Qt imports the Windows ICU API. A Poppler/Conda directory on PATH can
    # otherwise supply an incompatible ICU DLL with the same basename.
    windows = Path(os.environ.get("WINDIR", r"C:\Windows"))
    icu = windows / "System32/icuuc.dll"
    if icu.is_file():
        command.extend(["--add-binary", f"{icu};."])
    for package in ["akshare", "streamlit", "plotly", "matplotlib", "scipy", "IPython", "pyarrow",
                    "pytest", "tkinter", "PyQt5", "PyQt6", "PySide2", "numba", "notebook"]:
        command.extend(["--exclude-module", package])
    command.append(str(ROOT / "start_desktop.py"))
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    environment["PATH"] = os.pathsep.join([
        str(Path(sys.executable).parent), str(Path(sys.base_prefix)),
        str(windows / "System32"), str(windows),
    ])
    result = subprocess.run(command, cwd=ROOT, env=environment)
    if result.returncode:
        return result.returncode
    print(f"Desktop executable: {ROOT / 'dist/StockShape/StockShape.exe'}")
    print("Keep the whole StockShape folder inside this project; workers use project .venv and data.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
