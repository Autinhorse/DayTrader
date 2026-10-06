"""
Massive K 线下载工具（图形界面）。

运行:  .venv\\Scripts\\python download_gui.py   （或双击 run_downloader.bat）
秒线按月分文件: data/<TICKER>/<TICKER>_1second_adj/<年-月>.parquet；其它周期一个文件，
如 data/<TICKER>/<TICKER>_1minute_adj.parquet。重复下载时只补缺少的日期。
"""

import json
import os
import re
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QDate, QThread, Signal
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDateEdit, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QVBoxLayout, QWidget,
)

from market_data.massive import (
    DATA_DIR, Cancelled, DownloadRequest, MassiveClient, MassiveError, download,
)

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"     # 保存 API Key 和上次的输入，已加入 .gitignore

# 显示名 -> (multiplier, timespan)
BAR_SIZES = {
    "1 秒": (1, "second"), "5 秒": (5, "second"), "10 秒": (10, "second"),
    "30 秒": (30, "second"),
    "1 分钟": (1, "minute"), "5 分钟": (5, "minute"), "15 分钟": (15, "minute"),
    "30 分钟": (30, "minute"),
    "1 小时": (1, "hour"), "1 日": (1, "day"), "1 周": (1, "week"),
}


def load_config():
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


class Worker(QThread):
    log = Signal(str)
    progress = Signal(int, int)

    def __init__(self, api_key, requests_):
        super().__init__()
        self.stop_event = threading.Event()
        self.client = MassiveClient(api_key, stop_event=self.stop_event)
        self.requests_ = requests_

    def run(self):
        n = len(self.requests_)
        for i, req in enumerate(self.requests_, 1):
            self.log.emit(f"[{i}/{n}] 下载 {req.ticker} {req.span_name} {req.start} ~ {req.end} ...")
            try:
                download(self.client, req, DATA_DIR, log=self.log.emit,
                         progress=self.progress.emit)
            except Cancelled:
                self.log.emit("已停止。")
                return
            except MassiveError as e:
                self.log.emit(f"{req.ticker} 失败: {e}")
            except Exception as e:      # 其它意外错误也显示出来，不让线程悄悄退出
                self.log.emit(f"{req.ticker} 出错: {type(e).__name__}: {e}")
        self.log.emit("全部完成。")


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Massive K 线下载")
        self.resize(720, 560)
        self.worker = None
        cfg = load_config()

        self.api_key = QLineEdit(os.environ.get("MASSIVE_API_KEY") or cfg.get("api_key", ""))
        self.api_key.setEchoMode(QLineEdit.Password)
        self.save_key = QCheckBox("保存到 config.json")
        self.save_key.setChecked(bool(cfg.get("api_key")))
        key_row = QHBoxLayout()
        key_row.addWidget(self.api_key)
        key_row.addWidget(self.save_key)

        self.tickers = QLineEdit(cfg.get("tickers", "SPY"))
        self.tickers.setPlaceholderText("多个代码用逗号或空格分隔，如 SPY, QQQ, AAPL")

        today = QDate.currentDate()
        self.start = QDateEdit(today.addDays(-7))
        self.end = QDateEdit(today)
        for w in (self.start, self.end):
            w.setCalendarPopup(True)
            w.setDisplayFormat("yyyy-MM-dd")
            w.setMaximumDate(today)
        date_row = QHBoxLayout()
        date_row.addWidget(self.start)
        date_row.addWidget(self.end)
        for label, days in (("近1周", 7), ("近1月", 30), ("近1年", 365)):
            b = QPushButton(label)
            b.clicked.connect(lambda _, d=days: self.set_range(d))
            date_row.addWidget(b)

        self.bar_size = QComboBox()
        self.bar_size.addItems(BAR_SIZES)

        self.adjusted = QCheckBox("拆股复权")
        self.adjusted.setChecked(True)
        note = QLabel("秒线按月分文件，其它周期一个文件；包含盘前 / 盘中 / 盘后全部时段（session 列标记），已下载的日期自动跳过")
        note.setStyleSheet("color: gray")
        note.setWordWrap(True)

        form = QFormLayout()
        form.addRow("API Key", key_row)
        form.addRow("股票代码", self.tickers)
        form.addRow("日期 (起 / 止)", date_row)
        form.addRow("K 线周期", self.bar_size)
        form.addRow("选项", self.adjusted)
        form.addRow("", note)
        box = QGroupBox("下载设置")
        box.setLayout(form)

        self.start_btn = QPushButton("开始下载")
        self.start_btn.clicked.connect(self.start_download)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_download)
        open_btn = QPushButton("打开 data 目录")
        open_btn.clicked.connect(lambda: (DATA_DIR.mkdir(exist_ok=True), os.startfile(DATA_DIR)))
        btn_row = QHBoxLayout()
        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.stop_btn)
        btn_row.addStretch()
        btn_row.addWidget(open_btn)

        self.bar = QProgressBar()
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)

        layout = QVBoxLayout(self)
        layout.addWidget(box)
        layout.addLayout(btn_row)
        layout.addWidget(self.bar)
        layout.addWidget(self.log)

    def set_range(self, days):
        today = QDate.currentDate()
        self.start.setDate(today.addDays(-days))
        self.end.setDate(today)

    def start_download(self):
        key = self.api_key.text().strip()
        # 去重并保持输入顺序
        tickers = list(dict.fromkeys(t.upper() for t in re.split(r"[,\s，]+", self.tickers.text()) if t))
        start = self.start.date().toPython()
        end = self.end.date().toPython()
        multiplier, timespan = BAR_SIZES[self.bar_size.currentText()]

        error = None
        if not key:
            error = "请输入 API Key"
        elif not tickers:
            error = "请输入股票代码"
        elif start > end:
            error = "开始日期不能晚于结束日期"
        if error:
            QMessageBox.warning(self, "提示", error)
            return

        cfg = {"tickers": self.tickers.text()}
        if self.save_key.isChecked():
            cfg["api_key"] = key
        CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

        reqs = [DownloadRequest(t, start, end, multiplier, timespan, self.adjusted.isChecked())
                for t in tickers]
        self.worker = Worker(key, reqs)
        self.worker.log.connect(self.log.appendPlainText)
        self.worker.progress.connect(lambda i, n: (self.bar.setMaximum(n), self.bar.setValue(i)))
        self.worker.finished.connect(self.on_finished)
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.bar.setValue(0)
        self.worker.start()

    def stop_download(self):
        if self.worker:
            self.worker.stop_event.set()
            self.stop_btn.setEnabled(False)

    def on_finished(self):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.worker.stop_event.set()
            self.worker.wait(5000)
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())
