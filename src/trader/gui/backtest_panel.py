"""回测表单（DESIGN.md 10.7 第 1、6 条）：选择策略、填写参数（表单由参数模型自动生成）、日期区间、
撮合与成本假设、风控阈值，点击运行。参数格子里填多个取值（5,9,13 或 5:20:5）即发起参数扫描；
可以设定样本外起始日，把区间切成样本内和样本外两段。

回测在子进程里运行（python -m trader.backtest.worker），界面不卡，可以取消。
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from typing import Any

from PySide6.QtCore import QDate, QProcess, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from trader.backtest.config import BacktestConfig
from trader.backtest.sweep import expand_grid
from trader.brokers.sim.matcher import SimConfig
from trader.core.clock import WallClock
from trader.core.timeutil import from_ns
from trader.gui.forms import ModelForm
from trader.gui.services import Services, today
from trader.oms.risk import RiskLimits
from trader.strategy.base import get_strategy


def qdate(e: QDateEdit) -> date:
    return e.date().toPython()  # type: ignore[return-value]


def _date_edit(d: date) -> QDateEdit:
    e = QDateEdit(QDate(d.year, d.month, d.day))
    e.setCalendarPopup(True)
    e.setDisplayFormat("yyyy-MM-dd")
    return e


class BacktestPanel(QWidget):
    finished = Signal(list, object)  # (run_ids, sweep_id)

    def __init__(self, sv: Services) -> None:
        super().__init__()
        self.sv = sv
        self.proc: QProcess | None = None
        self.params_form: ModelForm | None = None

        self.strategy = QComboBox()
        self.desc = QLabel("")
        self.desc.setWordWrap(True)
        self.desc.setStyleSheet("color: gray")
        reload_btn = QPushButton("重新加载策略")
        self.params_box = QGroupBox("策略参数（数字可填多个取值进行参数扫描：5,9,13 或 5:20:5）")
        self.params_lay = QVBoxLayout(self.params_box)

        now = today()
        self.start = _date_edit(date(now.year, now.month, 1) if now.day > 1 else now)
        self.end = _date_edit(now)
        self.session = QComboBox()
        self.session.addItem("只看常规时段", "rth")
        self.session.addItem("含盘前盘后", "extended")
        self.trade_ext = QCheckBox("允许盘前盘后开仓（只能用限价单）")
        self.hold_post = QCheckBox("持仓到盘后（20:00 前平仓）；不勾选则常规收盘前平仓")
        self.cash = QLineEdit("100000")
        self.split_on = QCheckBox("样本外从这天开始：")
        self.split = _date_edit(now)
        self.name = QLineEdit()
        self.name.setPlaceholderText("实验名称（可选）")
        self.notes = QLineEdit()
        self.notes.setPlaceholderText("备注")
        self.tags = QLineEdit()
        self.tags.setPlaceholderText("标签，逗号分隔")

        main = QFormLayout()
        srow = QHBoxLayout()
        srow.addWidget(self.strategy, 1)
        srow.addWidget(reload_btn)
        main.addRow("策略", srow)
        main.addRow("", self.desc)
        drow = QHBoxLayout()
        drow.addWidget(self.start)
        drow.addWidget(QLabel("～"))
        drow.addWidget(self.end)
        drow.addStretch()
        main.addRow("日期", drow)
        main.addRow("时段", self.session)
        main.addRow("", self.trade_ext)
        main.addRow("", self.hold_post)
        main.addRow("初始资金", self.cash)
        sp = QHBoxLayout()
        sp.addWidget(self.split_on)
        sp.addWidget(self.split)
        sp.addStretch()
        main.addRow("样本内/外", sp)
        main.addRow("名称", self.name)
        main.addRow("备注", self.notes)
        main.addRow("标签", self.tags)

        self.sim_form = ModelForm(SimConfig)
        sim_box = QGroupBox("撮合与成本假设")
        sim_box.setCheckable(True)
        sim_box.setChecked(False)
        QVBoxLayout(sim_box).addWidget(self.sim_form)
        sim_box.toggled.connect(self.sim_form.setVisible)
        self.sim_form.setVisible(False)
        self.risk_form = ModelForm(RiskLimits)
        risk_box = QGroupBox("风控阈值")
        risk_box.setCheckable(True)
        risk_box.setChecked(False)
        QVBoxLayout(risk_box).addWidget(self.risk_form)
        risk_box.toggled.connect(self.risk_form.setVisible)
        self.risk_form.setVisible(False)

        self.run_btn = QPushButton("运行回测")
        self.run_btn.setStyleSheet("font-weight: bold; padding: 6px")
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setEnabled(False)
        self.progress = QProgressBar()
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)

        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.addLayout(main)
        lay.addWidget(self.params_box)
        lay.addWidget(sim_box)
        lay.addWidget(risk_box)
        lay.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        btns = QHBoxLayout()
        btns.addWidget(self.run_btn, 1)
        btns.addWidget(self.cancel_btn)
        outer = QVBoxLayout(self)
        outer.addWidget(scroll, 1)
        outer.addLayout(btns)
        outer.addWidget(self.progress)
        outer.addWidget(self.log)
        self.log.setMaximumHeight(110)

        self.strategy.currentIndexChanged.connect(self._strategy_changed)
        reload_btn.clicked.connect(self._reload)
        self.run_btn.clicked.connect(self.run)
        self.cancel_btn.clicked.connect(self.cancel)
        self._fill_strategies()

    # ---------- 策略 ----------

    def _fill_strategies(self, keep: str | None = None) -> None:
        self.strategy.blockSignals(True)
        self.strategy.clear()
        for cls in self.sv.strategy_classes():
            self.strategy.addItem(cls.name, cls.name)
        self.strategy.blockSignals(False)
        if keep and self.strategy.findData(keep) >= 0:
            self.strategy.setCurrentIndex(self.strategy.findData(keep))
        self._strategy_changed()

    def _reload(self) -> None:
        errs = self.sv.reload_user_code()
        self._fill_strategies(self.strategy.currentData())
        if errs:
            QMessageBox.warning(self, "加载失败", "\n".join(f"{k}：{v}" for k, v in errs.items()))

    def _strategy_changed(self) -> None:
        name = self.strategy.currentData()
        if not name:
            return
        cls = get_strategy(name)
        self.desc.setText(cls.description)
        if self.params_form is not None:
            self.params_form.setParent(None)  # type: ignore[call-overload]
        self.params_form = ModelForm(cls.Params, sweep=True)
        self.params_lay.addWidget(self.params_form)

    def load_config(self, cfg: dict[str, Any]) -> None:
        """用一次回测的配置填表（例如从实验列表“以此为模板”）。"""
        name = cfg.get("strategy")
        if not name:
            return
        if self.strategy.findData(name) >= 0:
            self.strategy.setCurrentIndex(self.strategy.findData(name))
        if self.params_form is not None:
            self.params_form.setParent(None)  # type: ignore[call-overload]
        self.params_form = ModelForm(get_strategy(name).Params, cfg.get("params", {}), sweep=True)
        self.params_lay.addWidget(self.params_form)
        for key, edit in (("start", self.start), ("end", self.end)):
            d = date.fromisoformat(cfg[key])
            edit.setDate(QDate(d.year, d.month, d.day))
        self.session.setCurrentIndex(0 if cfg.get("session", "rth") == "rth" else 1)
        self.trade_ext.setChecked(bool(cfg.get("trade_extended")))
        self.hold_post.setChecked(bool(cfg.get("hold_post")))
        self.cash.setText(str(cfg.get("initial_cash", 100000)))
        self.name.setText(cfg.get("name", ""))

    # ---------- 运行 ----------

    def _job(self) -> dict[str, Any]:
        assert self.params_form is not None
        grid = self.params_form.sweep_grid()
        cfg = BacktestConfig(
            name=self.name.text().strip(),
            strategy=self.strategy.currentData(),
            params=self.params_form.values(),
            start=qdate(self.start),
            end=qdate(self.end),
            session=self.session.currentData(),
            trade_extended=self.trade_ext.isChecked(),
            hold_post=self.hold_post.isChecked(),
            initial_cash=float(self.cash.text() or 100000),
            sim=SimConfig.model_validate(self.sim_form.values()),
            risk=RiskLimits.model_validate(self.risk_form.values()),
        )
        return {
            "config": json.loads(cfg.model_dump_json()),
            "grid": grid,
            "split": qdate(self.split).isoformat() if self.split_on.isChecked() else None,
            "notes": self.notes.text().strip(),
            "tags": self.tags.text().strip(),
            "data_dir": str(self.sv.data_dir),
            "project_dir": str(self.sv.project_dir),
            "out_root": str(self.sv.runs_dir),
        }

    def run(self) -> None:
        try:
            job = self._job()
        except (ValueError, TypeError) as e:
            QMessageBox.warning(self, "设置有误", str(e))
            return
        n = len(expand_grid(job["grid"])) * (2 if job["split"] else 1)
        if (
            n > 1
            and QMessageBox.question(self, "参数扫描", f"将运行 {n} 次回测，继续吗？")
            != QMessageBox.StandardButton.Yes
        ):
            return
        jobs_dir = self.sv.runs_dir / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        path = jobs_dir / f"job-{from_ns(WallClock(lambda _e: None).now()):%Y%m%d-%H%M%S}.json"
        path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        self.log.appendPlainText(f"开始：{job['config']['strategy']}，{n} 次回测 …")
        self.progress.setValue(0)
        self.proc = QProcess(self)
        self.proc.setProgram(sys.executable)
        self.proc.setArguments(["-m", "trader.backtest.worker", str(path)])
        self.proc.setWorkingDirectory(str(self.sv.project_dir))
        env = self.proc.processEnvironment()
        env.insert("PYTHONIOENCODING", "utf-8")
        self.proc.setProcessEnvironment(env)
        self.proc.readyReadStandardOutput.connect(self._read)
        self.proc.finished.connect(self._done)
        self._result: tuple[list[str], str | None] | None = None
        self._err = ""
        self.run_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.proc.start()

    def _read(self) -> None:
        assert self.proc is not None
        while self.proc.canReadLine():
            line = bytes(self.proc.readLine().data()).decode("utf-8", errors="replace").strip()
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("type") == "progress":
                self.progress.setMaximum(msg["total"])
                self.progress.setValue(msg["done"])
                self.progress.setFormat(f"%v/%m  {msg.get('label', '')}")
            elif msg.get("type") == "done":
                self._result = (msg["run_ids"], msg.get("sweep_id"))
            elif msg.get("type") == "error":
                self._err = msg["message"]
                self.log.appendPlainText(msg.get("trace", ""))

    def _done(self) -> None:
        self.run_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        if self._result is not None:
            ids, sweep = self._result
            self.progress.setValue(self.progress.maximum())
            self.log.appendPlainText(
                f"完成：{len(ids)} 次回测" + (f"（扫描 {sweep}）" if sweep else "")
            )
            self.finished.emit(ids, sweep)
        elif self._err:
            self.log.appendPlainText(f"出错：{self._err}")
            QMessageBox.warning(self, "回测出错", self._err)
        else:
            self.log.appendPlainText("已取消")

    def cancel(self) -> None:
        """取消：结束子进程及其派生的并行回测进程。"""
        if self.proc is None or self.proc.state() == QProcess.ProcessState.NotRunning:
            return
        pid = self.proc.processId()
        if sys.platform == "win32" and pid:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
        else:
            self.proc.kill()
