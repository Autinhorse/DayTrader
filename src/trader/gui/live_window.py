"""实盘版界面（DESIGN.md 11、12.4）：独立进程，通过本机端口连接交易引擎（trader.live.control）。

界面只显示引擎推送的状态、把按钮变成命令发回去；关掉界面不影响交易，重新打开即可看到当前状态。
- 顶部横幅按运行方式着色：本地模拟（蓝）、IBKR 模拟账户（橙）、实盘（红）。
- 心跳：超过 5 秒没有收到引擎状态就显示“引擎无响应”。
- 策略：开启 / 暂停（只能减仓）/ 停止（撤单，持仓转手动）。
- 四个风控按钮：停止新开仓、撤销全部挂单、全部平仓、停止全部策略；另有立即对账、按券商持仓重置。
- 手动下单与策略订单走同一条路径（风控、落盘）。会改变持仓或挂单的操作都先确认。
- 重要告警（断线、行情中断、对账不一致、风控）弹出桌面通知。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import polars as pl
from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtNetwork import QAbstractSocket, QTcpSocket
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDockWidget,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from trader.core.clock import WallClock
from trader.core.timeutil import from_ns
from trader.gui.tables import FrameTable

PROFILE_STYLE = {
    "local_paper": ("#1565c0", "本地模拟盘（实时行情 + 本地撮合，不向券商下单）"),
    "broker_paper": ("#e65100", "IBKR 模拟账户（真实下单到模拟账户）"),
    "live": ("#b71c1c", "实盘（真实资金）"),
}
IMPORTANT = {
    "CONNECTION_LOST", "FEED_INTERRUPTED", "FEED_DEGRADED", "RECONCILE_MISMATCH", "risk",
}  # fmt: skip
HEARTBEAT_S = 5.0

ORDER_COLS = [
    ("id", "订单号"), ("symbol", "标的"), ("side", "方向"), ("qty", "数量"), ("type", "类型"),
    ("limit", "限价"), ("stop", "止损价"), ("filled", "已成交"), ("avg", "均价"),
    ("status", "状态"),
    ("role", "角色"), ("source", "来源"), ("reason", "原因"), ("reject", "拒绝原因"),
]  # fmt: skip


def _short(symbols: list[str], n: int = 5) -> str:
    """标的多时只列前几个（完整列表在提示框里）。"""
    if len(symbols) <= n:
        return "、".join(symbols)
    return "、".join(symbols[:n]) + f" 等 {len(symbols)} 个"


def _time(ns: Any) -> str:
    if not isinstance(ns, int) or ns <= 0:
        return ""
    return from_ns(ns).strftime("%H:%M:%S")  # 纽约时间


class LiveWindow(QMainWindow):
    def __init__(self, profile: str, port: int, host: str = "127.0.0.1") -> None:
        super().__init__()
        self.profile, self.host, self.port = profile, host, port
        color, title = PROFILE_STYLE.get(profile, ("#424242", profile))
        self.setWindowTitle(f"DayTrader 实盘版 — {title}")
        self.resize(1400, 860)
        self._clock = WallClock(lambda _ev: None)
        self.status: dict[str, Any] = {}
        self.last_status_at = 0.0
        self._next_id = 0
        self._shown: dict[str, int] = {}
        self.sent: list[dict[str, Any]] = []  # 发出的命令（测试用）
        self.confirm: Callable[[str], bool] = self._confirm
        self.notify: Callable[[str, str], None] = self._notify

        central = QWidget()
        lay = QVBoxLayout(central)
        self.banner = QLabel(title)
        self.banner.setStyleSheet(
            f"background:{color}; color:white; font-size:16px; font-weight:bold; padding:6px;"
        )
        lay.addWidget(self.banner)
        self.health = QLabel("正在连接引擎…")
        self.health.setStyleSheet("font-size:13px; padding:2px;")
        lay.addWidget(self.health)
        self.summary = QLabel("")
        self.summary.setStyleSheet("font-size:13px; padding:2px;")
        lay.addWidget(self.summary)
        row = QHBoxLayout()
        row.addWidget(self._strategy_box())
        row.addWidget(self._risk_box())
        row.addWidget(self._order_box())
        lay.addLayout(row)
        self.positions = FrameTable()
        self.positions.doubleClicked.connect(self._fill_close_form)
        lay.addWidget(QLabel("持仓（双击填入平仓单）"))
        lay.addWidget(self.positions, 1)
        self.setCentralWidget(central)

        self.open_orders = FrameTable()
        self.orders = FrameTable()
        self.fills = FrameTable()
        self.market = FrameTable()
        self.alerts = QListWidget()
        self.alerts.setWordWrap(True)
        cancel = QPushButton("撤销选中的挂单")
        cancel.clicked.connect(self._cancel_selected)
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.addWidget(self.open_orders)
        v.addWidget(cancel)
        self._dock("挂单", w, Qt.DockWidgetArea.RightDockWidgetArea)
        self._dock("告警与提示", self.alerts, Qt.DockWidgetArea.RightDockWidgetArea)
        self._dock("今日订单", self.orders, Qt.DockWidgetArea.BottomDockWidgetArea)
        self._dock("成交", self.fills, Qt.DockWidgetArea.BottomDockWidgetArea)
        self._dock("行情", self.market, Qt.DockWidgetArea.BottomDockWidgetArea)
        self.statusBar().showMessage("准备就绪")

        self.sock = QTcpSocket(self)
        self.sock.readyRead.connect(self._read)
        self.sock.connected.connect(lambda: self.statusBar().showMessage("已连接引擎"))
        self._buf = b""
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(
            self.style().standardIcon(self.style().StandardPixmap.SP_MessageBoxWarning)
        )
        self.tray.show()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(1000)
        self._restore()

    # ---------- 布局 ----------

    def _dock(self, title: str, widget: QWidget, area: Qt.DockWidgetArea) -> None:
        d = QDockWidget(title, self)
        d.setObjectName(f"dock_{title}")
        d.setWidget(widget)
        self.addDockWidget(area, d)

    def _strategy_box(self) -> QGroupBox:
        box = QGroupBox("策略")
        g = QGridLayout(box)
        self.strategy_label = QLabel("-")
        g.addWidget(self.strategy_label, 0, 0, 1, 3)
        self.btn_start = QPushButton("开启")
        self.btn_pause = QPushButton("暂停（只减仓）")
        self.btn_stop = QPushButton("停止")
        self.force = QCheckBox("数据有缺口时强制开启")
        self.btn_start.clicked.connect(
            lambda: self.send("start_strategy", force=self.force.isChecked())
        )
        self.btn_pause.clicked.connect(lambda: self.send("pause_strategy"))
        self.btn_stop.clicked.connect(
            lambda: (
                self.confirm("停止策略：撤销它的挂单，持仓转为手动（不平仓）。继续？")
                and self.send("stop_strategy")
            )
        )
        g.addWidget(self.btn_start, 1, 0)
        g.addWidget(self.btn_pause, 1, 1)
        g.addWidget(self.btn_stop, 1, 2)
        g.addWidget(self.force, 2, 0, 1, 3)
        return box

    def _risk_box(self) -> QGroupBox:
        box = QGroupBox("风控")
        g = QGridLayout(box)
        self.btn_halt = QPushButton("停止新开仓")
        self.btn_halt.setCheckable(True)
        self.btn_halt.clicked.connect(lambda on: self.send("halt_new", on=on))
        b_cancel = QPushButton("撤销全部挂单")
        b_cancel.clicked.connect(
            lambda: (
                self.confirm("撤销全部挂单（包括括号单的保护单）。继续？")
                and self.send("cancel_all")
            )
        )
        b_flat = QPushButton("全部平仓")
        b_flat.setStyleSheet("color:#b71c1c; font-weight:bold;")
        b_flat.clicked.connect(
            lambda: (
                self.confirm("全部平仓：先撤销全部挂单，确认后按实际持仓平仓。继续？")
                and self.send("flatten_all")
            )
        )
        b_stop_all = QPushButton("停止全部策略")
        b_stop_all.clicked.connect(
            lambda: (
                self.confirm("停止全部策略（撤单，持仓转手动）。继续？")
                and self.send("stop_all_strategies")
            )
        )
        b_rec = QPushButton("立即对账")
        b_rec.clicked.connect(lambda: self.send("reconcile"))
        self.btn_resync = QPushButton("按券商持仓重置")
        self.btn_resync.clicked.connect(
            lambda: (
                self.confirm(
                    "把本地持仓改成券商的数字（只改本地记录，不下单），归属转为手动。确认券商的持仓是对的？"
                )
                and self.send("resync_to_broker")
            )
        )
        for i, b in enumerate(
            [self.btn_halt, b_cancel, b_flat, b_stop_all, b_rec, self.btn_resync]
        ):
            g.addWidget(b, i // 2, i % 2)
        return box

    def _order_box(self) -> QGroupBox:
        box = QGroupBox("手动下单")
        f = QFormLayout(box)
        self.o_symbol = QComboBox()
        self.o_symbol.setEditable(True)
        self.o_side = QComboBox()
        self.o_side.addItems(["BUY", "SELL"])
        self.o_qty = QSpinBox()
        self.o_qty.setRange(1, 100_000)
        self.o_type = QComboBox()
        self.o_type.addItems(["MKT", "LMT", "STP"])
        self.o_limit, self.o_stop, self.o_tp, self.o_sl = (QDoubleSpinBox() for _ in range(4))
        for sp in (self.o_limit, self.o_stop, self.o_tp, self.o_sl):
            sp.setRange(0, 1_000_000)
            sp.setDecimals(2)
            sp.setSpecialValueText("-")
        self.o_rth = QCheckBox("盘前盘后（只能限价）")
        f.addRow("标的", self.o_symbol)
        side = QHBoxLayout()
        side.addWidget(self.o_side)
        side.addWidget(self.o_qty)
        side.addWidget(self.o_type)
        f.addRow("方向 / 数量 / 类型", side)
        px = QHBoxLayout()
        px.addWidget(self.o_limit)
        px.addWidget(self.o_stop)
        f.addRow("限价 / 触发价", px)
        br = QHBoxLayout()
        br.addWidget(self.o_tp)
        br.addWidget(self.o_sl)
        f.addRow("括号单 止盈 / 止损", br)
        f.addRow(self.o_rth)
        b = QPushButton("下单")
        b.clicked.connect(self._submit_order)
        f.addRow(b)
        return box

    # ---------- 命令 ----------

    def send(self, cmd: str, **kw: Any) -> bool:
        self._next_id += 1
        msg = {"id": self._next_id, "cmd": cmd, **kw}
        self.sent.append(msg)
        if self.sock.state() != QAbstractSocket.SocketState.ConnectedState:
            self.statusBar().showMessage("没有连接到引擎，命令没有发出")
            return False
        self.sock.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
        self.statusBar().showMessage(f"已发送：{cmd}")
        return True

    def order_command(self) -> dict[str, Any]:
        def v(sp: QDoubleSpinBox) -> float | None:
            return sp.value() or None

        return {
            "symbol": self.o_symbol.currentText().strip().upper(),
            "side": self.o_side.currentText(),
            "qty": self.o_qty.value(),
            "type": self.o_type.currentText(),
            "limit": v(self.o_limit),
            "stop": v(self.o_stop),
            "take_profit": v(self.o_tp),
            "stop_loss": v(self.o_sl),
            "outside_rth": self.o_rth.isChecked(),
        }

    def _submit_order(self) -> None:
        o = self.order_command()
        px = o["limit"] or o["stop"] or "市价"
        bracket = (
            f"，止盈 {o['take_profit']} 止损 {o['stop_loss']}"
            if o["take_profit"] or o["stop_loss"]
            else ""
        )
        if self.confirm(f"{o['side']} {o['qty']} {o['symbol']} {o['type']} {px}{bracket}。下单？"):
            self.send("manual_order", **o)

    def _cancel_selected(self) -> None:
        ids = set()
        for i in self.open_orders.selectedIndexes():
            r = self.open_orders.row_at(i)
            if r:
                ids.add(r["id"])
        for coid in sorted(ids):
            self.send("cancel_order", id=coid)

    def _fill_close_form(self, index: Any) -> None:
        r = self.positions.row_at(index)
        if not r:
            return
        self.o_symbol.setCurrentText(r["symbol"])
        self.o_side.setCurrentText("SELL" if r["qty"] > 0 else "BUY")
        self.o_qty.setValue(abs(int(r["qty"])))
        self.o_type.setCurrentText("MKT")

    def _confirm(self, text: str) -> bool:
        return (
            QMessageBox.question(
                self, "确认", text, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            == QMessageBox.StandardButton.Yes
        )

    def _notify(self, title: str, text: str) -> None:
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.showMessage(title, text, QSystemTrayIcon.MessageIcon.Warning, 8000)

    # ---------- 连接与消息 ----------

    def _tick(self) -> None:
        if self.sock.state() == QAbstractSocket.SocketState.UnconnectedState:
            self.sock.connectToHost(self.host, self.port)
        age = self._clock.now() / 1e9 - self.last_status_at if self.last_status_at else None
        if age is None:
            self.health.setText(
                f"正在连接引擎 127.0.0.1:{self.port}…（引擎未启动时先运行 trader-live）"
            )
            self.health.setStyleSheet("color:#b71c1c; font-size:13px; padding:2px;")
        elif age > HEARTBEAT_S:
            self.health.setText(f"引擎无响应：{age:.0f} 秒没有收到状态")
            self.health.setStyleSheet(
                "background:#b71c1c; color:white; font-size:13px; padding:2px;"
            )

    def _read(self) -> None:
        self._buf += bytes(self.sock.readAll().data())
        *lines, self._buf = self._buf.split(b"\n")
        for line in lines:
            if line.strip():
                self.handle(json.loads(line))

    def handle(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "status":
            self.apply_status(msg)
        elif kind == "alert":
            self.add_alert(msg)
        elif kind == "result":
            text = ("完成：" if msg.get("ok") else "未成功：") + str(msg.get("msg", ""))
            self.statusBar().showMessage(text, 15000)
            if not msg.get("ok"):
                QApplication.beep()

    def add_alert(self, a: dict[str, Any]) -> None:
        sym = f" [{a['symbol']}]" if a.get("symbol") else ""
        self.alerts.insertItem(0, f"{_time(a.get('ts'))} {a.get('kind')}{sym} {a.get('detail')}")
        if self.alerts.count() > 500:
            self.alerts.takeItem(500)
        if a.get("kind") in IMPORTANT:
            self.alerts.item(0).setForeground(Qt.GlobalColor.red)
            self.notify(f"DayTrader {a.get('kind')}", f"{sym} {a.get('detail')}".strip())

    def apply_status(self, st: dict[str, Any]) -> None:
        self.status, self.last_status_at = st, self._clock.now() / 1e9
        if not st.get("ready"):
            self.health.setText("引擎正在启动（连接、补数）…")
            self.health.setStyleSheet("color:#e65100; font-size:13px; padding:2px;")
            return
        conn = "已连接 IB" if st.get("connected") else "与 IB 断开"
        problems = []
        if not st.get("connected"):
            problems.append("断线")
        if st.get("stale"):
            problems.append(f"行情中断 {_short(st['stale'])}")
        if st.get("gaps"):
            problems.append(f"数据缺口 {_short(st['gaps'])}")
        if st.get("mismatch"):
            problems.append(f"对账不一致 {list(st['mismatch'])}")
        session = {"pre": "盘前", "regular": "常规时段", "post": "盘后", None: "休市"}.get(
            st.get("session"), str(st.get("session"))
        )
        text = f"引擎正常　{conn}　账户 {st.get('account') or '-'}　{session}"
        if problems:
            text += "　⚠ " + "；".join(problems)
        self.health.setText(text)
        self.health.setToolTip(
            "\n".join(f"{k}：{', '.join(st.get(k) or [])}" for k in ("stale", "gaps") if st.get(k))
        )
        bad = bool(problems)
        self.health.setStyleSheet(
            ("background:#fff3e0; color:#b71c1c;" if bad else "color:#2e7d32;")
            + "font-size:13px; padding:2px;"
        )
        self.summary.setText(
            f"权益 {st.get('equity', 0):,.2f}　当日盈亏 {st.get('day_pnl', 0):+,.2f}　"
            f"已实现 {st.get('realized', 0):+,.2f}　未实现 {st.get('unrealized', 0):+,.2f}　"
            f"手续费 {st.get('commission', 0):,.2f}　待发请求 {st.get('queued', 0)}"
        )
        name = st.get("strategy") or "（配置里没有策略）"
        if not st.get("strategy_running"):
            state = "未开启"
        elif st.get("strategy_paused"):
            state = "已暂停（只能减仓）"
        else:
            state = "运行中"
        self.strategy_label.setText(f"{name}：{state}")
        self.btn_halt.setChecked(bool(st.get("halt_all_new")))
        self.btn_halt.setText(
            "已停止新开仓（点击恢复）" if st.get("halt_all_new") else "停止新开仓"
        )
        self.btn_resync.setEnabled(st.get("profile") != "local_paper")
        syms = st.get("symbols", [])
        if [self.o_symbol.itemText(i) for i in range(self.o_symbol.count())] != syms:
            cur = self.o_symbol.currentText()
            self.o_symbol.clear()
            self.o_symbol.addItems(syms)
            if cur:
                self.o_symbol.setCurrentText(cur)
        self._tables(st)

    def _set(
        self,
        name: str,
        table: FrameTable,
        rows: list[dict[str, Any]],
        cols: list[tuple[str, str]],
        **kw: Any,
    ) -> None:
        h = hash(json.dumps(rows, sort_keys=True, default=str))
        if self._shown.get(name) == h:
            return  # 没变化就不重画（保留选中和滚动位置）
        self._shown[name] = h
        df = (
            pl.DataFrame(rows, infer_schema_length=None)
            if rows
            else pl.DataFrame({c: [] for c, _ in cols})
        )
        table.set_frame(df, cols, **kw)

    def _tables(self, st: dict[str, Any]) -> None:
        pos = [{"symbol": s, **p} for s, p in sorted(st.get("positions", {}).items())]
        self._set("pos", self.positions, pos, [
            ("symbol", "标的"), ("qty", "数量"), ("avg_cost", "成本"), ("last", "最新价"),
            ("unrealized", "未实现盈亏"), ("owner", "归属"),
        ], color_col="unrealized")  # fmt: skip
        self._set("open", self.open_orders, st.get("open_orders", []), ORDER_COLS)
        self._set("orders", self.orders, list(reversed(st.get("orders", []))), ORDER_COLS)
        fills = [{**f, "time": _time(f.get("ts"))} for f in reversed(st.get("fills", []))]
        self._set("fills", self.fills, fills, [
            ("time", "时间"), ("symbol", "标的"), ("side", "方向"), ("qty", "数量"),
            ("price", "价格"), ("id", "订单号"), ("exec_id", "成交编号"),
        ])  # fmt: skip
        stale, gaps = set(st.get("stale", [])), set(st.get("gaps", []))
        market = [
            {
                "symbol": s,
                "last": st.get("last_price", {}).get(s),
                "bar": st.get("last_bar", {}).get(s, ""),
                "state": "中断" if s in stale else ("有缺口" if s in gaps else "正常"),
            }
            for s in st.get("symbols", [])
        ]
        self._set("market", self.market, market, [
            ("symbol", "标的"), ("last", "最新价"), ("bar", "最近 bar"), ("state", "状态"),
        ])  # fmt: skip

    # ---------- 布局保存 ----------

    def _restore(self) -> None:
        s = QSettings("DayTrader", f"live_ui_{self.profile}")
        geo, state = s.value("geometry"), s.value("state")
        if geo is not None:
            self.restoreGeometry(geo)  # type: ignore[arg-type]
        if state is not None:
            self.restoreState(state)  # type: ignore[arg-type]
        else:  # 第一次打开：右侧挂单和告警栏给足宽度（窗口显示后才能调整）
            QTimer.singleShot(300, self._default_sizes)

    def _default_sizes(self) -> None:
        docks = {d.windowTitle(): d for d in self.findChildren(QDockWidget)}
        right = [docks["挂单"], docks["告警与提示"]]
        self.resizeDocks(right, [480, 480], Qt.Orientation.Horizontal)
        self.resizeDocks(right, [330, 420], Qt.Orientation.Vertical)
        bottom = [docks["今日订单"], docks["成交"], docks["行情"]]
        self.resizeDocks(bottom, [260, 260, 260], Qt.Orientation.Vertical)

    def closeEvent(self, event: Any) -> None:  # noqa: N802
        s = QSettings("DayTrader", f"live_ui_{self.profile}")
        s.setValue("geometry", self.saveGeometry())
        s.setValue("state", self.saveState())
        self.tray.hide()
        super().closeEvent(event)
