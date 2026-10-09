"""实盘版界面入口：trader-live-ui（独立进程，只通过本机端口连接 trader-live）。

uv run trader-live-ui --profile broker_paper
"""

from __future__ import annotations

import argparse
import sys

from trader.config import project_dir
from trader.live.config import ConfigError, load_live_config


def main() -> int:
    p = argparse.ArgumentParser(description="DayTrader 实盘版界面")
    p.add_argument("--profile", required=True, choices=["local_paper", "broker_paper", "live"])
    p.add_argument("--port", type=int, help="默认按配置文件")
    args = p.parse_args()
    port = args.port
    if port is None:
        try:
            port = load_live_config(project_dir() / "config" / f"{args.profile}.yaml").ui_port
        except (ConfigError, FileNotFoundError) as exc:
            print(f"配置错误：{exc}")
            return 2
    from PySide6.QtWidgets import QApplication

    from trader.gui.live_window import LiveWindow

    app = QApplication(sys.argv)
    app.setApplicationName("DayTrader 实盘版")
    w = LiveWindow(args.profile, port)
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
