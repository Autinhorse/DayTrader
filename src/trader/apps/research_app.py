"""研究版入口：trader-research（DESIGN.md 12.1、决策 0005）。

启动 PySide6 桌面程序：图表、回测、结果、实验对比、参数扫描、数据管理。不会发出任何真实订单
（研究版不包含 IBKR 执行器）。
"""

from __future__ import annotations

import sys


def main() -> int:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication

    from trader.config import data_dir, project_dir
    from trader.gui.main_window import MainWindow

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName("trader-research")
    app.setFont(QFont("Microsoft YaHei UI", 9))
    w = MainWindow(project_dir(), data_dir())
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
