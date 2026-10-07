"""
行情数据管理（独立窗口）。研究版主程序 trader-research 里的“数据”面板是同一个界面。

运行:  双击 run_downloader.bat，或  uv run python download_gui.py
"""

import os
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parent
os.environ.setdefault("TRADER_HOME", str(ROOT))

from trader.gui.data_panel import DataPanel  # noqa: E402

if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = DataPanel(ROOT)
    w.setWindowTitle("行情数据管理（Massive）")
    w.resize(900, 780)
    w.show()
    sys.exit(app.exec())
