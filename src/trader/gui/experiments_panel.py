"""实验列表与对比（DESIGN.md 10.7 第 5、6 条）。

- 列表：按策略、文字（名称/备注/标签/参数）筛选；双击打开结果；可以改名、加备注和标签；
  “以此为模板”把配置填回回测表单。
- 对比：选中两次或多次回测，并排比较统计、叠加权益曲线，并列出它们在参数、日期、撮合假设、
  数据指纹上的不同之处。
- 参数扫描：显示参数对比表（样本内 / 样本外），双击一行打开对应的单次回测。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import polars as pl
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from trader.backtest import experiments as ex
from trader.gui.chart import ChartView
from trader.gui.results import daily_equity
from trader.gui.services import Services
from trader.gui.tables import FrameTable

COLUMNS = [
    ("name", "名称"),
    ("strategy", "策略"),
    ("params", "参数"),
    ("start", "开始"),
    ("end", "结束"),
    ("trades", "交易"),
    ("net_pnl", "净盈亏"),
    ("sample", "样本"),
    ("tags", "标签"),
    ("notes", "备注"),
    ("created", "运行时间"),
    ("run_id", "编号"),
]


def _params_text(p: str | None) -> str:
    try:
        d = json.loads(p or "{}")
    except ValueError:
        return p or ""
    return ", ".join(f"{k}={v}" for k, v in d.items())


class ExperimentsPanel(QWidget):
    open_run = Signal(str)  # 运行目录
    use_as_template = Signal(dict)  # 回测配置
    compare_runs = Signal(list)  # 运行目录列表

    def __init__(self, sv: Services) -> None:
        super().__init__()
        self.sv = sv
        self.index = sv.runs_dir / "index.sqlite"
        self.strategy = QComboBox()
        self.text = QLineEdit()
        self.text.setPlaceholderText("按名称、备注、标签、参数筛选")
        refresh = QPushButton("刷新")
        top = QHBoxLayout()
        top.addWidget(QLabel("策略"))
        top.addWidget(self.strategy)
        top.addWidget(self.text, 1)
        top.addWidget(refresh)
        self.table = FrameTable()
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        open_btn = QPushButton("打开结果")
        cmp_btn = QPushButton("比较所选")
        edit_btn = QPushButton("命名 / 备注 / 标签…")
        tpl_btn = QPushButton("以此为模板")
        sweep_btn = QPushButton("查看所属参数扫描")
        btns = QHBoxLayout()
        for b in (open_btn, cmp_btn, edit_btn, tpl_btn, sweep_btn):
            btns.addWidget(b)
        btns.addStretch()
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.table, 1)
        lay.addLayout(btns)

        refresh.clicked.connect(self.refresh)
        self.strategy.currentIndexChanged.connect(lambda _i: self.refresh())
        self.text.textChanged.connect(lambda _t: self.refresh())
        self.table.doubleClicked.connect(lambda idx: self._open(self.table.row_at(idx)))
        open_btn.clicked.connect(lambda: self._open(self._current()))
        cmp_btn.clicked.connect(self._compare)
        edit_btn.clicked.connect(self._edit)
        tpl_btn.clicked.connect(self._template)
        sweep_btn.clicked.connect(self._sweep)
        self.refresh()

    def refresh(self) -> None:
        rows = ex.list_runs(self.index, self.strategy.currentData(), self.text.text().strip())
        cur = self.strategy.currentData()
        self.strategy.blockSignals(True)
        self.strategy.clear()
        self.strategy.addItem("全部", None)
        for s in sorted({r["strategy"] for r in ex.list_runs(self.index)}):
            self.strategy.addItem(s, s)
        if cur is not None and self.strategy.findData(cur) >= 0:
            self.strategy.setCurrentIndex(self.strategy.findData(cur))
        self.strategy.blockSignals(False)
        for r in rows:
            r["params"] = _params_text(r.get("params"))
            r["sample"] = {"in": "样本内", "out": "样本外"}.get(r.get("sample") or "", "")
        df = (
            pl.DataFrame(rows, infer_schema_length=None)
            if rows
            else pl.DataFrame({c: [] for c, _ in COLUMNS})
        )
        self.table.set_frame(df, COLUMNS, color_col="net_pnl")

    def _selected(self) -> list[dict[str, Any]]:
        idxs = {i.row(): i for i in self.table.selectionModel().selectedRows()}
        return [r for r in (self.table.row_at(i) for i in idxs.values()) if r]

    def _current(self) -> dict[str, Any] | None:
        sel = self._selected()
        return sel[0] if sel else None

    def _open(self, row: dict[str, Any] | None) -> None:
        if row:
            self.open_run.emit(row["path"])

    def _compare(self) -> None:
        sel = self._selected()
        if len(sel) < 2:
            QMessageBox.information(self, "比较", "请按住 Ctrl 选中两次或多次回测")
            return
        self.compare_runs.emit([r["path"] for r in sel])

    def _edit(self) -> None:
        row = self._current()
        if not row:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("命名 / 备注 / 标签")
        name, notes, tags = (
            QLineEdit(row.get("name") or ""),
            QLineEdit(row.get("notes") or ""),
            QLineEdit(row.get("tags") or ""),
        )
        f = QFormLayout(dlg)
        f.addRow("名称", name)
        f.addRow("备注", notes)
        f.addRow("标签", tags)
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        f.addRow(bb)
        if dlg.exec():
            ex.update(
                self.index, row["run_id"], name=name.text(), notes=notes.text(), tags=tags.text()
            )
            self.refresh()

    def _template(self) -> None:
        row = self._current()
        if row:
            cfg = json.loads((Path(row["path"]) / "config.json").read_text(encoding="utf-8"))[
                "config"
            ]
            self.use_as_template.emit(cfg)

    def _sweep(self) -> None:
        row = self._current()
        if not row or not row.get("sweep_id"):
            QMessageBox.information(self, "参数扫描", "这次回测不属于参数扫描")
            return
        show_sweep(self.sv, row["sweep_id"], self, self.open_run)


def show_sweep(sv: Services, sweep_id: str, parent: QWidget, open_signal: Any) -> None:
    path = sv.runs_dir / "sweeps" / f"{sweep_id}.parquet"
    if not path.exists():
        QMessageBox.warning(parent, "参数扫描", f"找不到 {path}")
        return
    SweepDialog(sv, sweep_id, pl.read_parquet(path), open_signal, parent).show()


class SweepDialog(QDialog):
    """参数对比表：每组参数一行，样本内和样本外的主要统计；双击打开单次回测。"""

    LABELS = {
        "trades": "交易",
        "net_pnl": "净盈亏",
        "win_rate": "胜率",
        "profit_factor": "利润因子",
        "max_drawdown": "最大回撤",
        "sharpe": "夏普",
    }

    def __init__(
        self, sv: Services, sweep_id: str, table: pl.DataFrame, open_signal: Any, parent: QWidget
    ):
        super().__init__(parent)
        self.setWindowTitle(f"参数扫描 {sweep_id}")
        self.resize(1000, 500)
        self.sv, self.open_signal = sv, open_signal
        cols = []
        for c in table.columns:
            if c.startswith("param_"):
                cols.append((c, c.removeprefix("param_")))
        for prefix, label in (("", ""), ("in_", "样本内 "), ("out_", "样本外 ")):
            for k, name in self.LABELS.items():
                if f"{prefix}{k}" in table.columns:
                    cols.append((f"{prefix}{k}", f"{label}{name}"))
        self.t = FrameTable()
        self.t.set_frame(
            table,
            cols,
            {
                c: (lambda v: "-" if v is None else f"{v * 100:.1f}%")
                for c, _ in cols
                if c.endswith("win_rate")
            },
        )
        hint = QLabel(
            "双击一行打开这组参数的回测（有样本外时打开样本外）。样本内最好、样本外明显变差的参数组合，多半是过拟合。"
        )
        hint.setWordWrap(True)
        lay = QVBoxLayout(self)
        lay.addWidget(hint)
        lay.addWidget(self.t)
        self.t.doubleClicked.connect(self._open)

    def _open(self, idx) -> None:  # noqa: ANN001
        row = self.t.row_at(idx)
        if not row:
            return
        rid = row.get("out_run_id") or row.get("run_id") or row.get("in_run_id")
        if rid:
            self.open_signal.emit(str(self.sv.runs_dir / rid))


class CompareWindow(QWidget):
    """多次回测并排比较：统计、权益曲线、配置差别。"""

    def __init__(self, runs: list[ex.LoadedRun]) -> None:
        super().__init__()
        self.setWindowTitle("回测对比")
        self.resize(1100, 700)
        names = [r.config.get("name") or r.run_id for r in runs]
        stats = QTableWidget(len(ex.KEY_METRICS), len(runs))
        stats.setHorizontalHeaderLabels(names)
        stats.setVerticalHeaderLabels([m[0] for m in ex.KEY_METRICS])
        for j, r in enumerate(runs):
            for i, (_n, path, f) in enumerate(ex.KEY_METRICS):
                it = QTableWidgetItem(ex.format_metric(ex.metric(r.summary, path), f))
                it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                stats.setItem(i, j, it)
        stats.resizeColumnsToContents()
        diffs = ex.config_differences(runs)
        dt = QTableWidget(len(diffs), len(runs))
        dt.setHorizontalHeaderLabels(names)
        dt.setVerticalHeaderLabels([k for k, _ in diffs])
        for i, (_k, vals) in enumerate(diffs):
            for j, v in enumerate(vals):
                dt.setItem(
                    i,
                    j,
                    QTableWidgetItem(
                        json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
                    ),
                )
        dt.resizeColumnsToContents()
        chart = ChartView()
        series = [{"name": n, "data": daily_equity(r)} for r, n in zip(runs, names, strict=True)]
        chart.call("setLines", {"title": "收盘权益变化（相对初始资金）", "series": series})
        left = QSplitter(Qt.Orientation.Vertical)
        left.addWidget(stats)
        diff_w = QWidget()
        dl = QVBoxLayout(diff_w)
        dl.addWidget(QLabel("配置与数据的不同之处" + ("（完全相同）" if not diffs else "")))
        dl.addWidget(dt)
        left.addWidget(diff_w)
        split = QSplitter()
        split.addWidget(left)
        split.addWidget(chart)
        split.setSizes([500, 600])
        lay = QVBoxLayout(self)
        lay.addWidget(split)
