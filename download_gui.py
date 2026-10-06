"""
行情数据管理（图形界面）：从 Massive 下载 1 秒 bar（原始价格）到 data/bars/1s/。

运行:  双击 run_downloader.bat，或  uv run python download_gui.py
两个功能：
  1. 已有标的：更新到最新交易日，或往前补一段历史；
  2. 新增标的：输入代码和起始日期下载，可同时加入 config/universe.yaml。
下载是增量的：已有的日期自动跳过，中间缺的日期会补上。逻辑在 trader.data.download，
命令行 `trader data download/update` 和阶段 4 的网页"数据管理"都调用同一套代码。
"""

import os
import re
import sys
import threading
from datetime import date
from pathlib import Path

from PySide6.QtCore import QDate, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDateEdit,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

ROOT = Path(__file__).resolve().parent
os.environ.setdefault("TRADER_HOME", str(ROOT))

from trader.config import data_dir, load_universe  # noqa: E402
from trader.core.clock import WallClock  # noqa: E402
from trader.core.timeutil import ny_date  # noqa: E402
from trader.core.trading_calendar import TradingCalendar  # noqa: E402
from trader.data.catalog import Catalog  # noqa: E402
from trader.data.download import download_symbol, refresh_corporate_actions  # noqa: E402
from trader.data.massive import MassiveClient, find_api_key  # noqa: E402
from trader.data.store import BarStore  # noqa: E402

ENV_FILE = ROOT / ".env"
UNIVERSE_FILE = ROOT / "config" / "universe.yaml"
DEFAULT_START = QDate(2024, 1, 1)


def save_api_key(key):
    """写入 .env 的 MASSIVE_API_KEY（.env 不进 git），保留其他行。"""
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    lines = [ln for ln in lines if not ln.strip().startswith("MASSIVE_API_KEY")]
    lines.append(f"MASSIVE_API_KEY={key}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def add_to_universe(symbols):
    """把新代码追加到 universe.yaml 末尾（文本追加，保留原有注释），返回实际新增的代码。"""
    existing = set(load_universe(UNIVERSE_FILE).names)
    new = [s for s in symbols if s not in existing]
    if new:
        text = UNIVERSE_FILE.read_text(encoding="utf-8").rstrip("\n")
        text += "\n" + "".join(f"  - {s}\n" for s in new)
        UNIVERSE_FILE.write_text(text, encoding="utf-8")
    return new


class Worker(QThread):
    """jobs: [(代码, 起始日期, 结束日期)]，逐个标的下载。"""

    log = Signal(str)
    progress = Signal(str, int, int)

    def __init__(self, api_key, jobs):
        super().__init__()
        self.stop_event = threading.Event()
        self.client = MassiveClient(api_key, stop_event=self.stop_event)
        self.jobs = jobs

    def run(self):
        cal = TradingCalendar()
        catalog = Catalog(data_dir() / "catalog.sqlite")
        store = BarStore(data_dir())
        clock = WallClock(lambda _e: None)
        problems = 0
        try:
            for i, (sym, start, end) in enumerate(self.jobs, 1):
                if self.stop_event.is_set():
                    break
                self.log.emit(f"[{i}/{len(self.jobs)}] {sym} {start} ~ {end}")
                try:
                    res = download_symbol(
                        self.client, store, catalog, cal, clock, sym, start, end,
                        log=self.log.emit, progress=self.progress.emit,
                    )
                    problems += len(res.failed) + len(res.rejected)
                    if (res.stored or res.empty) and not self.stop_event.is_set():
                        n = refresh_corporate_actions(self.client, data_dir(), sym)
                        self.log.emit(f"{sym}: 拆股/分红记录 {n} 条")
                except Exception as e:  # 显示出来，不让线程悄悄退出
                    problems += 1
                    self.log.emit(f"{sym} 出错：{type(e).__name__}: {e}")
        finally:
            catalog.close()
        if self.stop_event.is_set():
            self.log.emit("已停止。已写入的日期不会丢失，再次下载会从缺的日期继续。")
        elif problems:
            self.log.emit(f"完成，但有 {problems} 个日期失败或未通过校验；再点一次同样的按钮会重试。")
        else:
            self.log.emit("全部完成。")


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("行情数据管理（Massive 1 秒 bar）")
        self.resize(860, 760)
        self.worker = None
        self.cal = TradingCalendar()

        # API Key
        self.api_key = QLineEdit(find_api_key(ROOT) or "")
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.save_key = QCheckBox("保存到 .env")
        self.save_key.setChecked(True)
        key_row = QHBoxLayout()
        key_row.addWidget(QLabel("API Key"))
        key_row.addWidget(self.api_key)
        key_row.addWidget(self.save_key)

        # 1. 已有标的
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["代码", "起始日期", "结束日期", "有数据天数", "缺的交易日"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)

        self.update_btn = QPushButton("更新到最新交易日")
        self.update_btn.setToolTip("从各标的已有的起始日期下载到今天；中间缺的日期也会补上")
        self.update_btn.clicked.connect(self.update_existing)
        self.back_date = QDateEdit(DEFAULT_START)
        self.back_date.setCalendarPopup(True)
        self.back_date.setDisplayFormat("yyyy-MM-dd")
        self.back_btn = QPushButton("往前补到此日期")
        self.back_btn.clicked.connect(self.extend_back)
        refresh_btn = QPushButton("刷新列表")
        refresh_btn.clicked.connect(self.refresh_table)
        ex_row = QHBoxLayout()
        ex_row.addWidget(self.update_btn)
        ex_row.addSpacing(20)
        ex_row.addWidget(self.back_date)
        ex_row.addWidget(self.back_btn)
        ex_row.addStretch()
        ex_row.addWidget(refresh_btn)
        hint = QLabel("不选任何行 = 对全部标的操作；按住 Ctrl 或 Shift 可多选。")
        hint.setStyleSheet("color: gray")
        ex_layout = QVBoxLayout()
        ex_layout.addWidget(self.table)
        ex_layout.addWidget(hint)
        ex_layout.addLayout(ex_row)
        ex_box = QGroupBox("1. 已有标的")
        ex_box.setLayout(ex_layout)

        # 2. 新增标的
        self.new_symbols = QLineEdit()
        self.new_symbols.setPlaceholderText("多个代码用逗号或空格分隔，如 AVGO, CRM")
        self.new_start = QDateEdit(DEFAULT_START)
        self.new_start.setCalendarPopup(True)
        self.new_start.setDisplayFormat("yyyy-MM-dd")
        self.add_universe = QCheckBox("同时加入标的清单 universe.yaml")
        self.add_universe.setChecked(True)
        self.add_btn = QPushButton("下载新标的")
        self.add_btn.clicked.connect(self.add_new)
        new_row = QHBoxLayout()
        new_row.addWidget(QLabel("代码"))
        new_row.addWidget(self.new_symbols, 1)
        new_row.addWidget(QLabel("从"))
        new_row.addWidget(self.new_start)
        new_row.addWidget(QLabel("到今天"))
        new_row2 = QHBoxLayout()
        new_row2.addWidget(self.add_universe)
        new_row2.addStretch()
        new_row2.addWidget(self.add_btn)
        new_layout = QVBoxLayout()
        new_layout.addLayout(new_row)
        new_layout.addLayout(new_row2)
        new_box = QGroupBox("2. 新增标的")
        new_box.setLayout(new_layout)

        # 运行状态
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_download)
        open_btn = QPushButton("打开 data 目录")
        open_btn.clicked.connect(lambda: os.startfile(data_dir()))
        self.status = QLabel("")
        run_row = QHBoxLayout()
        run_row.addWidget(self.stop_btn)
        run_row.addWidget(self.status, 1)
        run_row.addWidget(open_btn)
        self.bar = QProgressBar()
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        note = QLabel("只下载 1 秒 bar、原始价格（不复权），含盘前盘后；其他周期由系统聚合生成。"
                      "当天要等盘后结束 30 分钟后才会下载。")
        note.setStyleSheet("color: gray")
        note.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addLayout(key_row)
        layout.addWidget(ex_box, 3)
        layout.addWidget(new_box)
        layout.addWidget(note)
        layout.addLayout(run_row)
        layout.addWidget(self.bar)
        layout.addWidget(self.log, 2)

        self.refresh_table()

    # ---------- 列表 ----------

    def refresh_table(self):
        catalog = Catalog(data_dir() / "catalog.sqlite")
        try:
            symbols = list(dict.fromkeys(catalog.symbols() + load_universe(UNIVERSE_FILE).names))
            rows = []
            for sym in symbols:
                have = [p.day for p in catalog.partitions(sym) if p.rows > 0]
                known = catalog.known_days(sym)
                if have:
                    tdays = [d.day for d in self.cal.trading_days(have[0], have[-1])]
                    missing = sum(1 for d in tdays if d not in known)
                    rows.append((sym, str(have[0]), str(have[-1]), str(len(have)), str(missing)))
                else:
                    rows.append((sym, "-", "-", "0", "-"))
        finally:
            catalog.close()
        self.table.setRowCount(len(rows))
        for r, values in enumerate(rows):
            for c, v in enumerate(values):
                item = QTableWidgetItem(v)
                if c:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(r, c, item)

    def selected_rows(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        if not rows:
            rows = list(range(self.table.rowCount()))
        out = []
        for r in rows:
            sym_item, start_item = self.table.item(r, 0), self.table.item(r, 1)
            if sym_item and start_item:
                out.append((sym_item.text(), start_item.text()))
        return out

    # ---------- 操作 ----------

    def today(self):
        return ny_date(WallClock(lambda _e: None).now())

    def update_existing(self):
        jobs = []
        for sym, first in self.selected_rows():
            start = date.fromisoformat(first) if first != "-" else DEFAULT_START.toPython()
            jobs.append((sym, start, self.today()))
        self.run_jobs(jobs)

    def extend_back(self):
        target = self.back_date.date().toPython()
        jobs = []
        for sym, first in self.selected_rows():
            end = date.fromisoformat(first) if first != "-" else self.today()
            if target < end:
                jobs.append((sym, target, end))
        if not jobs:
            QMessageBox.information(self, "提示", f"所选标的的数据都已经从 {target} 或更早开始。")
            return
        self.run_jobs(jobs)

    def add_new(self):
        text = self.new_symbols.text()
        symbols = list(dict.fromkeys(t.upper() for t in re.split(r"[,\s，]+", text) if t))
        if not symbols:
            QMessageBox.warning(self, "提示", "请输入股票代码")
            return
        start = self.new_start.date().toPython()
        if self.add_universe.isChecked():
            added = add_to_universe(symbols)
            if added:
                self.log.appendPlainText(f"已加入 universe.yaml：{', '.join(added)}")
        self.run_jobs([(s, start, self.today()) for s in symbols])

    def run_jobs(self, jobs):
        key = self.api_key.text().strip()
        if not key:
            QMessageBox.warning(self, "提示", "请输入 API Key")
            return
        if self.save_key.isChecked() and key != find_api_key(ROOT):
            save_api_key(key)
        self.worker = Worker(key, jobs)
        self.worker.log.connect(self.log.appendPlainText)
        self.worker.progress.connect(self.on_progress)
        self.worker.finished.connect(self.on_finished)
        self.set_busy(True)
        self.bar.setValue(0)
        self.worker.start()

    def on_progress(self, sym, done, total):
        self.bar.setMaximum(total)
        self.bar.setValue(done)
        self.status.setText(f"{sym}: {done}/{total} 天")

    def set_busy(self, busy):
        for b in (self.update_btn, self.back_btn, self.add_btn):
            b.setEnabled(not busy)
        self.stop_btn.setEnabled(busy)

    def stop_download(self):
        if self.worker:
            self.worker.stop_event.set()
            self.stop_btn.setEnabled(False)

    def on_finished(self):
        self.set_busy(False)
        self.status.setText("")
        self.refresh_table()

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.worker.stop_event.set()
            self.worker.wait(10000)
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())
