"""数据管理（DESIGN.md 11.2 数据页）：下载与更新、覆盖与校验报告、Massive 与 IBKR 对比报告。

下载逻辑在 trader.data.download；命令行 `trader data download/update` 调用同一套代码。
"""

# ruff: noqa: E501
# pyright: basic

import os
import re
import threading
from datetime import date
from pathlib import Path

from PySide6.QtCore import QDate, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDateEdit,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from trader.config import data_dir, load_universe
from trader.core.clock import WallClock
from trader.core.timeutil import ny_date
from trader.core.trading_calendar import TradingCalendar
from trader.data.catalog import Catalog
from trader.data.download import download_symbol, refresh_corporate_actions
from trader.data.massive import MassiveClient, find_api_key
from trader.data.report import data_report
from trader.data.store import BarStore

DEFAULT_START = QDate(2024, 1, 1)


def save_api_key(env_file, key):
    """写入 .env 的 MASSIVE_API_KEY（.env 不进 git），保留其他行。"""
    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    lines = [ln for ln in lines if not ln.strip().startswith("MASSIVE_API_KEY")]
    lines.append(f"MASSIVE_API_KEY={key}")
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def add_to_universe(universe_file, symbols):
    """把新代码追加到 universe.yaml 末尾（文本追加，保留原有注释），返回实际新增的代码。"""
    existing = set(load_universe(universe_file).names)
    new = [s for s in symbols if s not in existing]
    if new:
        text = universe_file.read_text(encoding="utf-8").rstrip("\n")
        text += "\n" + "".join(f"  - {s}\n" for s in new)
        universe_file.write_text(text, encoding="utf-8")
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
        stores = {k: BarStore(data_dir(), k) for k in ("1s", "1m")}
        clock = WallClock(lambda _e: None)
        problems = 0
        try:
            for i, (sym, start, end) in enumerate(self.jobs, 1):
                if self.stop_event.is_set():
                    break
                self.log.emit(f"[{i}/{len(self.jobs)}] {sym} {start} ~ {end}")
                try:
                    changed = False
                    for kind in ("1s", "1m"):  # 1 秒 bar 和官方 1 分钟 bar
                        res = download_symbol(
                            self.client,
                            stores[kind],
                            catalog,
                            cal,
                            clock,
                            sym,
                            start,
                            end,
                            log=self.log.emit,
                            progress=self.progress.emit,
                            kind=kind,
                        )
                        problems += len(res.failed) + len(res.rejected)
                        changed = changed or bool(res.stored or res.empty)
                    if changed and not self.stop_event.is_set():
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
            self.log.emit(
                f"完成，但有 {problems} 个日期失败或未通过校验；再点一次同样的按钮会重试。"
            )
        else:
            self.log.emit("全部完成。")


class DownloadWidget(QWidget):
    """下载与更新：已有标的更新到最新 / 往前补；新增标的。"""

    def __init__(self, project_dir):
        super().__init__()
        self.root = Path(project_dir)
        self.env_file = self.root / ".env"
        self.universe_file = self.root / "config" / "universe.yaml"
        self.worker = None
        self.cal = TradingCalendar()

        # API Key
        self.api_key = QLineEdit(find_api_key(self.root) or "")
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.save_key = QCheckBox("保存到 .env")
        self.save_key.setChecked(True)
        key_row = QHBoxLayout()
        key_row.addWidget(QLabel("API Key"))
        key_row.addWidget(self.api_key)
        key_row.addWidget(self.save_key)

        # 1. 已有标的
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["代码", "起始日期", "结束日期", "有数据天数", "缺的交易日"]
        )
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
        note = QLabel(
            "下载 1 秒 bar 和官方 1 分钟 bar（成交量用），原始价格（不复权），含盘前盘后；"
            "其他周期由系统聚合生成。当天要等盘后结束 30 分钟后才会下载，"
            "8 小时内下载的算临时数据，下次更新时自动重新下载。"
        )
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
            symbols = list(
                dict.fromkeys(catalog.symbols() + load_universe(self.universe_file).names)
            )
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
        target: date = self.back_date.date().toPython()  # type: ignore[assignment]
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
            added = add_to_universe(self.universe_file, symbols)
            if added:
                self.log.appendPlainText(f"已加入 universe.yaml：{', '.join(added)}")
        self.run_jobs([(s, start, self.today()) for s in symbols])

    def run_jobs(self, jobs):
        key = self.api_key.text().strip()
        if not key:
            QMessageBox.warning(self, "提示", "请输入 API Key")
            return
        if self.save_key.isChecked() and key != find_api_key(self.root):
            save_api_key(self.env_file, key)
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


class DataPanel(QTabWidget):
    """数据页：下载与更新、校验报告、数据对比报告。"""

    def __init__(self, project_dir: Path) -> None:
        super().__init__()
        self.root = Path(project_dir)
        self.download = DownloadWidget(self.root)
        self.addTab(self.download, "下载与更新")

        rep = QWidget()
        rl = QVBoxLayout(rep)
        btn = QPushButton("生成覆盖与校验报告")
        self.report = QTextBrowser()
        self.report.setStyleSheet("font-family: Consolas, 'Microsoft YaHei'")
        rl.addWidget(btn)
        rl.addWidget(self.report)
        btn.clicked.connect(self._report)
        self.addTab(rep, "覆盖与校验报告")

        cmp = QWidget()
        cl = QVBoxLayout(cmp)
        self.files = QListWidget()
        self.cmp_text = QTextBrowser()
        self.cmp_text.setStyleSheet("font-family: Consolas, 'Microsoft YaHei'")
        split = QSplitter()
        split.addWidget(self.files)
        split.addWidget(self.cmp_text)
        split.setSizes([250, 700])
        hint = QLabel(
            "IBKR 录制与 Massive 的对比报告（runs/compare/ 与 docs/reports/）。录制用 tools/ib_record.py，生成报告用 trader data compare。"
        )
        hint.setWordWrap(True)
        cl.addWidget(hint)
        cl.addWidget(split)
        self.files.currentTextChanged.connect(self._show_file)
        self.addTab(cmp, "数据对比报告")
        self.currentChanged.connect(lambda i: self._list_files() if i == 2 else None)

    def _report(self) -> None:
        cal = TradingCalendar()
        catalog = Catalog(data_dir() / "catalog.sqlite")
        try:
            symbols = list(
                dict.fromkeys(
                    load_universe(self.root / "config" / "universe.yaml").names + catalog.symbols()
                )
            )
            self.report.setPlainText(data_report(data_dir(), catalog, cal, symbols))
        finally:
            catalog.close()

    def _list_files(self) -> None:
        self.files.clear()
        self._paths = {}
        for d in (self.root / "runs" / "compare", self.root / "docs" / "reports"):
            if d.exists():
                for p in sorted(d.glob("*.txt"), reverse=True):
                    self._paths[p.name] = p
                    self.files.addItem(p.name)

    def _show_file(self, name: str) -> None:
        p = getattr(self, "_paths", {}).get(name)
        if p is not None:
            self.cmp_text.setPlainText(p.read_text(encoding="utf-8"))
