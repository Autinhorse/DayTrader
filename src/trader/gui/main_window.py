"""研究版主窗口（决策 0005）：可停靠面板 + 任意多个图表窗口。

- 每个面板都可以拖动重新排列、叠成标签页、拖出主窗口成为独立窗口（可以放到另一块显示器），
  双击标题栏放回主窗口。
- 菜单“窗口 → 新建图表”打开新的图表；“布局 → 恢复默认布局”整理面板。
- 退出时保存窗口布局和打开的图表（标的、周期、指标），下次启动恢复。
- 顶部横幅标明当前是“研究（模拟）”，与以后的实盘版区分（DESIGN.md 11.3）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PySide6.QtCore import QByteArray, QSettings, Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtNetwork import QLocalServer
from PySide6.QtWidgets import (
    QDockWidget,
    QLabel,
    QMainWindow,
    QMessageBox,
    QToolBar,
    QWidget,
)

from trader.backtest.experiments import load_run
from trader.gui.backtest_panel import BacktestPanel
from trader.gui.chart import ChartWidget
from trader.gui.data_panel import DataPanel
from trader.gui.experiments_panel import CompareWindow, ExperimentsPanel, show_sweep
from trader.gui.ipc import IPC_NAME
from trader.gui.replay_panel import ReplayPanel
from trader.gui.results import ResultsPanel
from trader.gui.services import Services

BANNER_STYLE = "background: #3d5a80; color: white; font-weight: bold; padding: 4px 10px;"


class MainWindow(QMainWindow):
    def __init__(
        self, project_dir: Path, data_dir: Path, settings_path: Path | None = None
    ) -> None:
        super().__init__()
        self.setWindowTitle("研究（模拟） — 日内交易研究")
        self.resize(1600, 950)
        self.setDockOptions(
            QMainWindow.DockOption.AllowNestedDocks
            | QMainWindow.DockOption.AllowTabbedDocks
            | QMainWindow.DockOption.AnimatedDocks
            | QMainWindow.DockOption.GroupedDragging
        )
        # 左侧回测表单占满整个高度；右侧上方图表、下方结果
        self.setCorner(Qt.Corner.BottomLeftCorner, Qt.DockWidgetArea.LeftDockWidgetArea)
        self.sv = Services(project_dir, data_dir)
        self.settings = QSettings(
            str(settings_path or project_dir / "config" / "research_gui.ini"),
            QSettings.Format.IniFormat,
        )
        self.charts: dict[str, tuple[QDockWidget, ChartWidget]] = {}
        self._chart_seq = 0
        self._windows: list[QWidget] = []  # 对比窗口等，防止被回收

        # 顶部横幅放在工具栏里（不可移动），面板铺满窗口其余部分
        banner = QLabel(
            "研究（模拟）　数据：Massive 历史数据　成交：模拟撮合　不会发出任何真实订单　｜　"
            "面板可拖动标题栏重新排列，或拖出主窗口单独放置（双击标题栏放回）；“窗口”菜单新建图表"
        )
        banner.setStyleSheet(BANNER_STYLE)
        tb = QToolBar("模式")
        tb.setObjectName("banner")
        tb.setMovable(False)
        tb.addWidget(banner)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, tb)

        self.backtest = BacktestPanel(self.sv)
        self.results = ResultsPanel()
        self.experiments = ExperimentsPanel(self.sv)
        self.data = DataPanel(project_dir)
        self.replay = ReplayPanel(self.sv)
        self.d_backtest = self._dock(
            "backtest", "回测", self.backtest, Qt.DockWidgetArea.LeftDockWidgetArea
        )
        self.d_results = self._dock(
            "results", "回测结果", self.results, Qt.DockWidgetArea.BottomDockWidgetArea
        )
        self.d_experiments = self._dock(
            "experiments", "实验列表", self.experiments, Qt.DockWidgetArea.BottomDockWidgetArea
        )
        self.d_data = self._dock(
            "data", "数据管理", self.data, Qt.DockWidgetArea.BottomDockWidgetArea
        )
        self.d_replay = self._dock(
            "replay", "回放", self.replay, Qt.DockWidgetArea.BottomDockWidgetArea
        )
        self.tabifyDockWidget(self.d_results, self.d_experiments)
        self.tabifyDockWidget(self.d_experiments, self.d_data)
        self.tabifyDockWidget(self.d_data, self.d_replay)
        self.d_results.raise_()
        self.replay_charts: dict[str, QDockWidget] = {}
        self.replay.session_changed.connect(self._replay_loaded)

        self.backtest.finished.connect(self._backtest_done)
        self.results.trade_selected.connect(self._show_trade)
        self.experiments.open_run.connect(self.open_run)
        self.experiments.compare_runs.connect(self._compare)
        self.experiments.use_as_template.connect(self._template)

        self._menus()
        self._restore()
        self._start_ipc()
        self._first_show = self.settings.value("state") is None
        if self.sv.load_errors:
            QMessageBox.warning(
                self,
                "部分自定义代码加载失败",
                "\n".join(f"{k}：{v}" for k, v in self.sv.load_errors.items()),
            )

    # ---------- 面板 ----------

    def _dock(self, name: str, title: str, w: QWidget, area: Qt.DockWidgetArea) -> QDockWidget:
        d = QDockWidget(title, self)
        d.setObjectName(name)
        d.setWidget(w)
        d.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
            | QDockWidget.DockWidgetFeature.DockWidgetClosable
        )
        self.addDockWidget(area, d)
        return d

    def new_chart(
        self, config: dict[str, Any] | None = None, name: str | None = None
    ) -> ChartWidget:
        self._chart_seq += 1
        name = name or f"chart_{self._chart_seq}"
        while name in self.charts:
            self._chart_seq += 1
            name = f"chart_{self._chart_seq}"
        w = ChartWidget(self.sv, config)
        d = self._dock(name, f"图表 {w.title()}", w, Qt.DockWidgetArea.RightDockWidgetArea)
        w.title_changed.connect(lambda t, dock=d: dock.setWindowTitle(f"图表 {t}"))
        d.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        d.destroyed.connect(lambda _o=None, n=name: self.charts.pop(n, None))
        others = [dk for dk, _ in self.charts.values() if not dk.isFloating()]
        if others:
            self.splitDockWidget(others[-1], d, Qt.Orientation.Vertical)
        self.charts[name] = (d, w)
        return w

    def new_replay_chart(self, config: dict[str, Any] | None = None) -> None:
        """回放图表：数据截止到回放时钟。可以开多个（例如同一标的的 1 分钟和 5 分钟）。"""
        n = 1
        while f"replay_chart_{n}" in self.replay_charts:
            n += 1
        name = f"replay_chart_{n}"
        w = self.replay.new_chart(config)
        d = self._dock(name, w.title(), w, Qt.DockWidgetArea.RightDockWidgetArea)
        w.title_changed.connect(lambda t, dock=d: dock.setWindowTitle(t))
        d.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        d.destroyed.connect(lambda _o=None, k=name: self.replay_charts.pop(k, None))
        self.replay_charts[name] = d
        charts = [dk for dk, _ in self.charts.values() if not dk.isFloating()]
        if charts:
            self.tabifyDockWidget(charts[0], d)
        d.show()
        d.raise_()

    def _replay_loaded(self) -> None:
        if not self.replay_charts:
            self.new_replay_chart()
        for d in self.replay_charts.values():
            d.raise_()

    def _menus(self) -> None:
        win = self.menuBar().addMenu("窗口")
        a = QAction("新建图表", self)
        a.setShortcut("Ctrl+N")
        a.triggered.connect(lambda: self.new_chart())
        win.addAction(a)
        ra = QAction("新建回放图表", self)
        ra.triggered.connect(lambda: self.new_replay_chart())
        win.addAction(ra)
        win.addSeparator()
        for d in (self.d_backtest, self.d_results, self.d_experiments, self.d_data, self.d_replay):
            win.addAction(d.toggleViewAction())
        lay = self.menuBar().addMenu("布局")
        reset = QAction("恢复默认布局", self)
        reset.triggered.connect(self._default_layout)
        lay.addAction(reset)
        code = self.menuBar().addMenu("代码")
        reload_ = QAction("重新加载自定义指标和策略", self)
        reload_.triggered.connect(self._reload_code)
        code.addAction(reload_)

    def _default_layout(self) -> None:
        for d in (self.d_backtest, self.d_results, self.d_experiments, self.d_data, self.d_replay):
            d.setFloating(False)
            d.show()
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.d_backtest)
        for d in (self.d_results, self.d_experiments, self.d_data):
            self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, d)
        self.tabifyDockWidget(self.d_results, self.d_experiments)
        self.tabifyDockWidget(self.d_experiments, self.d_data)
        self.tabifyDockWidget(self.d_data, self.d_replay)
        for dk, _ in self.charts.values():
            dk.setFloating(False)
            self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dk)
        self._default_sizes()

    def showEvent(self, event) -> None:  # noqa: ANN001, N802
        super().showEvent(event)
        if self._first_show:  # 第一次启动：窗口显示后再设默认尺寸才生效
            self._first_show = False
            QTimer.singleShot(300, self._default_sizes)

    def _default_sizes(self) -> None:
        """左边回测表单约 440 宽；右侧上方图表约六成高度，下方结果约四成。"""
        # resizeDocks 要同时给出相邻两侧的面板才会生效
        w, h = self.width(), self.height()
        charts = [d for d, _ in self.charts.values() if not d.isFloating()]
        if charts:
            self.resizeDocks(
                [self.d_backtest, charts[0]], [440, w - 440], Qt.Orientation.Horizontal
            )
            self.resizeDocks(
                [charts[0], self.d_results], [int(h * 0.55), int(h * 0.4)], Qt.Orientation.Vertical
            )

    def _reload_code(self) -> None:
        errs = self.sv.reload_user_code()
        self.backtest._fill_strategies(self.backtest.strategy.currentData())
        if errs:
            QMessageBox.warning(self, "加载失败", "\n".join(f"{k}：{v}" for k, v in errs.items()))
        else:
            QMessageBox.information(self, "重新加载", "自定义指标和策略已重新加载")

    # ---------- 回测联动 ----------

    def _backtest_done(self, run_ids: list[str], sweep_id: str | None) -> None:
        self.experiments.refresh()
        if sweep_id:
            show_sweep(self.sv, sweep_id, self, self.experiments.open_run)
        elif run_ids:
            self.open_run(str(self.sv.runs_dir / run_ids[0]))

    def open_run(self, path: str) -> None:
        try:
            run = load_run(Path(path))
        except (OSError, KeyError, ValueError) as e:
            QMessageBox.warning(self, "打开失败", f"{path}\n{e}")
            return
        self.results.show_run(run)
        self.d_results.show()
        self.d_results.raise_()

    def _show_trade(self, run: Any, row: dict[str, Any]) -> None:
        targets = [w for _, w in self.charts.values() if w.link.isChecked()]
        if not targets:
            tf = str(run.config.get("params", {}).get("timeframe", "1m"))
            targets = [self.new_chart({"symbol": row["symbol"], "timeframe": tf})]
        label = run.config.get("name") or run.run_id
        for w in targets:
            w.show_trade(run.fills, label, row["symbol"], row["entry_time"], row["exit_time"])

    def _compare(self, paths: list[str]) -> None:
        runs = [load_run(Path(p)) for p in paths]
        w = CompareWindow(runs)
        w.show()
        self._windows.append(w)

    def _template(self, cfg: dict[str, Any]) -> None:
        self.backtest.load_config(cfg)
        self.d_backtest.show()
        self.d_backtest.raise_()

    # ---------- notebook 打开回测（res.open_in_ui） ----------

    def _start_ipc(self) -> None:
        QLocalServer.removeServer(IPC_NAME)
        self.ipc = QLocalServer(self)
        self.ipc.newConnection.connect(self._ipc_connection)
        self.ipc.listen(IPC_NAME)

    def _ipc_connection(self) -> None:
        sock = self.ipc.nextPendingConnection()
        if sock is None:
            return

        def read() -> None:
            while sock.canReadLine():
                path = bytes(sock.readLine().data()).decode("utf-8").strip()
                if path:
                    self.open_run(path)
                    self.raise_()
                    self.activateWindow()

        sock.readyRead.connect(read)

    # ---------- 布局保存与恢复 ----------

    def _restore(self) -> None:
        charts = json.loads(str(self.settings.value("charts", "[]")))
        if not charts:
            charts = [{"name": "chart_1", "config": {"symbol": "SPY", "timeframe": "1m"}}]
        for c in charts:
            self.new_chart(c.get("config"), c.get("name"))
        geo = self.settings.value("geometry")
        state = self.settings.value("state")
        if isinstance(geo, QByteArray):
            self.restoreGeometry(geo)
        if isinstance(state, QByteArray):
            self.restoreState(state)

    def save_layout(self) -> None:
        charts = [{"name": n, "config": w.config()} for n, (_d, w) in self.charts.items()]
        self.settings.setValue("charts", json.dumps(charts, ensure_ascii=False))
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("state", self.saveState())
        self.settings.sync()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self.save_layout()
        self.backtest.cancel()
        self.replay.pause()
        for w in self._windows:
            w.close()
        super().closeEvent(event)
