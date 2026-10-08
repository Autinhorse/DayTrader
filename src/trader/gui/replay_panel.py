"""回放面板（DESIGN.md 11.2 回放控制条）：看盘练习、手动模拟交易、复盘。

- 选日期、起始时间、标的，载入；播放 / 暂停、单步（到下一根 bar 收盘）、倍速、跳转。
- 向后跳转开始新的练习分支（空仓开始），原分支的记录保留，可在“分支统计”里查看。
- 回放中手动下单（市价、限价、平仓），或挂载策略；成交由模拟撮合给出，同样经过风控。
- 回放图表只显示回放时钟之前已经收盘的数据，正在形成的那根实时更新（不泄露未来数据）。
"""

from __future__ import annotations

from datetime import time
from typing import Any

import polars as pl
from PySide6.QtCore import QDate, QTime, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from trader.backtest.replay import ReplaySession, ReplaySettings
from trader.core.models import Bar
from trader.core.timeutil import NS_PER_MS, from_ns, ny_to_ns
from trader.gui.chart import ChartWidget
from trader.gui.forms import ModelForm
from trader.gui.services import Services, break_between_days, ny_seconds, today
from trader.gui.tables import FrameTable
from trader.strategy.base import get_strategy

SPEEDS = [1, 2, 5, 10, 30, 60, 120, 300, 900]
TICK_MS = 100  # 界面刷新间隔


def _bar_point(b: Bar) -> tuple[dict[str, Any], dict[str, Any]]:
    t = ny_seconds(b.ts_start)
    c = {"time": t, "open": b.open, "high": b.high, "low": b.low, "close": b.close}
    v = {"time": t, "value": b.volume, "color": "#26a69a55" if b.close >= b.open else "#ef535055"}
    return c, v


class ReplayChart(ChartWidget):
    """回放用的图表：数据来自回放会话，截止到回放时钟；播放时增量更新。"""

    def __init__(self, sv: Services, panel: ReplayPanel, config: dict[str, Any] | None = None):
        self.panel = panel
        self._pending: list[Bar] = []
        self._handles: list[Any] = []
        self._sent_ind: list[int] = []
        self._sent_fills = 0
        self._listener_engine: Any = None
        self.lookback = 2  # 往前显示几个交易日（向左拖到头时增加）
        super().__init__(sv, config)
        self.link.setChecked(False)
        self.link.setVisible(False)
        self.date_box.setVisible(False)

    @property
    def rs(self) -> ReplaySession | None:
        return self.panel.session

    def _on_bar(self, bar: Bar) -> None:
        if bar.symbol == self.symbol and bar.timeframe == self.timeframe:
            self._pending.append(bar)

    def load(self, end_day=None, n_days=None, focus=None) -> None:  # noqa: ANN001
        self.reload()

    def load_more(self) -> None:
        if self.rs is None or self.lookback >= 20:
            self.view.call("noMore")
            return
        self.lookback += 3
        self.reload()

    def reload(self) -> None:
        """整张图重画：载入、切换周期或标的、跳转到新分支时调用。"""
        rs = self.rs
        if rs is None:
            self.view.call(
                "setData",
                {"candles": [], "volume": [], "indicators": [], "title": "回放：请先载入"},
            )
            self.status.setText("在回放面板里选择日期和标的，点“载入”")
            return
        eng = rs.engine
        if self._listener_engine is not eng:  # 新分支：重新挂监听和活动指标
            if (
                self._listener_engine is not None
                and self._on_bar in self._listener_engine.bar_listeners
            ):
                self._listener_engine.bar_listeners.remove(self._on_bar)
            eng.bar_listeners.append(self._on_bar)
            self._listener_engine = eng
        rs.partial(self.symbol, self.timeframe)  # 订阅，之后才有形成中的 bar
        self._pending.clear()
        bars = rs.bars(self.symbol, self.timeframe, self.lookback)
        candles, volume = [], []
        for r in bars.iter_rows(named=True):
            t = ny_seconds(r["ts_start"])
            candles.append(
                {
                    "time": t,
                    "open": r["open"],
                    "high": r["high"],
                    "low": r["low"],
                    "close": r["close"],
                }
            )
            volume.append(
                {
                    "time": t,
                    "value": r["volume"],
                    "color": "#26a69a55" if r["close"] >= r["open"] else "#ef535055",
                }
            )
        inds = []
        self._handles = []
        self._sent_ind = []
        from trader.indicators.base import get_indicator

        for spec in self.indicators:
            df = rs.indicator_values(
                self.symbol, self.timeframe, spec.name, spec.params, self.lookback
            )
            cls = get_indicator(spec.name)
            t = [ny_seconds(x) for x in df["ts_start"].to_list()]
            outs = []
            for k, o in enumerate(cls.outputs):
                data = [
                    {"time": a, "value": b}
                    for a, b in zip(t, df[o.key].to_list(), strict=True)
                    if b is not None
                ]
                if cls.scope == "session":
                    break_between_days(data, step=o.plot == "hline")
                color = (spec.color if k == 0 and spec.color else None) or o.color
                outs.append(
                    {"key": o.key, "plot": o.plot, "pane": o.pane, "color": color, "data": data}
                )
            inds.append({"label": spec.label, "outputs": outs})
            h = rs.watch_indicator(self.symbol, self.timeframe, spec.name, spec.params)
            self._handles.append(h)
            self._sent_ind.append(len(h.values))
        p = rs.partial(self.symbol, self.timeframe)
        if p is not None:
            c, v = _bar_point(p)
            if not candles or candles[-1]["time"] < c["time"]:
                candles.append(c)
                volume.append(v)
        fills = rs.fills(self.symbol)
        self._sent_fills = fills.height
        last = candles[-1]["close"] if candles else 1.0
        self.view.call(
            "setData",
            {
                "title": f"回放 {self.symbol} · {self.timeframe} · {rs.branch.label}",
                "candles": candles,
                "volume": volume,
                "indicators": inds,
                "markers": self.sv.trade_markers(fills, self.symbol) if fills.height else [],
                "precision": 2 if last >= 1 else 4,
                "minMove": 0.01 if last >= 1 else 0.0001,
            },
        )
        self.status.setText(
            f"回放时钟 {from_ns(rs.now):%Y-%m-%d %H:%M:%S}（纽约时间）　只显示此刻之前已收盘的数据"
        )

    def tick(self, follow: bool) -> None:
        """播放中：追加新收盘的 bar、更新形成中的 bar、补指标点和新成交标记。"""
        rs = self.rs
        if rs is None:
            return
        candles, volume = [], []
        for b in self._pending:
            c, v = _bar_point(b)
            candles.append(c)
            volume.append(v)
        self._pending.clear()
        p = rs.partial(self.symbol, self.timeframe)
        if p is not None:
            c, v = _bar_point(p)
            candles.append(c)
            volume.append(v)
        ind_updates = []
        from trader.indicators.base import get_indicator

        for i, (spec, h) in enumerate(zip(self.indicators, self._handles, strict=False)):
            new = list(
                zip(
                    list(h.times)[self._sent_ind[i] :],
                    list(h.values)[self._sent_ind[i] :],
                    strict=True,
                )
            )
            self._sent_ind[i] = len(h.values)
            for o in get_indicator(spec.name).outputs:
                data = [
                    {"time": ny_seconds(t), "value": vals.get(o.key)}
                    for t, vals in new
                    if t and vals.get(o.key) is not None
                ]
                if data:
                    ind_updates.append({"i": i, "key": o.key, "data": data})
        markers = []
        fills = rs.fills(self.symbol)
        if fills.height > self._sent_fills:
            markers = self.sv.trade_markers(fills.slice(self._sent_fills), self.symbol)
            self._sent_fills = fills.height
        self.view.call(
            "update",
            {
                "candles": candles,
                "volume": volume,
                "indicators": ind_updates,
                "markers": markers,
                "follow": follow,
            },
        )
        self.status.setText(f"回放时钟 {from_ns(rs.now):%Y-%m-%d %H:%M:%S}（纽约时间）")

    def _changed(self) -> None:
        self.symbol = self.sym_box.currentText().strip().upper()
        self.timeframe = self.tf_box.currentText()
        self.session = self.sess_box.currentData()
        self.title_changed.emit(self.title())
        self.reload()

    def _render(self, prepended: int, focus=None) -> None:  # noqa: ANN001
        self.reload()

    def title(self) -> str:
        return f"回放 {self.symbol} {self.timeframe}"


class StrategyDialog(QDialog):
    def __init__(self, sv: Services, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("挂载策略")
        self.resize(460, 420)
        self.box = QComboBox()
        for cls in sv.strategy_classes():
            self.box.addItem(cls.name, cls.name)
        self.holder = QVBoxLayout()
        self.form: ModelForm | None = None
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(self.box)
        lay.addLayout(self.holder)
        lay.addStretch()
        lay.addWidget(bb)
        self.box.currentIndexChanged.connect(self._changed)
        self._changed()

    def _changed(self) -> None:
        if self.form is not None:
            self.form.setParent(None)  # type: ignore[call-overload]
        name = self.box.currentData()
        if name:
            self.form = ModelForm(get_strategy(name).Params)
            self.holder.addWidget(self.form)


class ReplayPanel(QWidget):
    session_changed = Signal()

    def __init__(self, sv: Services) -> None:
        super().__init__()
        self.sv = sv
        self.session: ReplaySession | None = None
        self.charts: list[ReplayChart] = []
        self.playing = False
        self.timer = QTimer(self)
        self.timer.setInterval(TICK_MS)
        self.timer.timeout.connect(self._tick)

        # 载入
        cov_end = today()
        self.day = QDateEdit(QDate(cov_end.year, cov_end.month, cov_end.day))
        self.day.setCalendarPopup(True)
        self.day.setDisplayFormat("yyyy-MM-dd")
        self.start = QTimeEdit(QTime(9, 30))
        self.start.setDisplayFormat("HH:mm")
        self.symbols = QLineEdit("SPY")
        self.symbols.setPlaceholderText("标的，逗号分隔")
        self.sess = QComboBox()
        self.sess.addItem("常规时段", "rth")
        self.sess.addItem("含盘前盘后", "extended")
        load = QPushButton("载入")
        r1 = QHBoxLayout()
        for lbl, w in (
            ("日期", self.day),
            ("开始", self.start),
            ("标的", self.symbols),
            ("时段", self.sess),
        ):
            r1.addWidget(QLabel(lbl))
            r1.addWidget(w)
        r1.addWidget(load)
        r1.addStretch()

        # 控制
        self.play_btn = QPushButton("▶ 播放")
        self.step_btn = QPushButton("单步")
        self.step_btn.setToolTip("推进到回放图表周期的下一根 bar 收盘")
        self.speed = QComboBox()
        for s in SPEEDS:
            self.speed.addItem(f"{s}x", s)
        self.speed.setCurrentIndex(SPEEDS.index(10))
        self.clock = QLabel("--:--:--")
        self.clock.setStyleSheet("font-size: 16px; font-weight: bold; font-family: Consolas")
        self.seek_time = QTimeEdit(QTime(10, 0))
        self.seek_time.setDisplayFormat("HH:mm:ss")
        seek = QPushButton("跳转")
        seek.setToolTip("向前跳：中间的行情全速处理；向后跳：开始新的练习分支（空仓）")
        self.branch = QLabel("")
        r2 = QHBoxLayout()
        for w in (
            self.play_btn,
            self.step_btn,
            QLabel("倍速"),
            self.speed,
            self.clock,
            QLabel("跳到"),
            self.seek_time,
            seek,
            self.branch,
        ):
            r2.addWidget(w)
        r2.addStretch()

        # 下单
        self.o_sym = QComboBox()
        self.qty = QSpinBox()
        self.qty.setRange(1, 100000)
        self.qty.setValue(100)
        self.otype = QComboBox()
        self.otype.addItem("市价", "MKT")
        self.otype.addItem("限价", "LMT")
        self.price = QDoubleSpinBox()
        self.price.setRange(0, 1_000_000)
        self.price.setDecimals(2)
        buy = QPushButton("买入")
        buy.setStyleSheet("color: #26a69a; font-weight: bold")
        sell = QPushButton("卖出")
        sell.setStyleSheet("color: #ef5350; font-weight: bold")
        flat = QPushButton("平仓")
        cancel_all = QPushButton("撤销挂单")
        self.strat_btn = QPushButton("挂载策略…")
        r3 = QHBoxLayout()
        for w in (
            QLabel("手动下单"),
            self.o_sym,
            QLabel("数量"),
            self.qty,
            self.otype,
            QLabel("限价"),
            self.price,
            buy,
            sell,
            flat,
            cancel_all,
            self.strat_btn,
        ):
            r3.addWidget(w)
        r3.addStretch()

        self.tabs = QTabWidget()
        self.pos_t = FrameTable()
        self.ord_t = FrameTable()
        self.fill_t = FrameTable()
        self.stat_t = FrameTable()
        self.tabs.addTab(self.pos_t, "持仓")
        self.tabs.addTab(self.ord_t, "挂单")
        self.tabs.addTab(self.fill_t, "成交")
        self.tabs.addTab(self.stat_t, "分支统计")

        lay = QVBoxLayout(self)
        lay.addLayout(r1)
        lay.addLayout(r2)
        lay.addLayout(r3)
        lay.addWidget(self.tabs, 1)

        load.clicked.connect(self.load)
        self.play_btn.clicked.connect(self.toggle_play)
        self.step_btn.clicked.connect(self.step)
        seek.clicked.connect(self.seek)
        buy.clicked.connect(lambda: self._order("BUY"))
        sell.clicked.connect(lambda: self._order("SELL"))
        flat.clicked.connect(self._flatten)
        cancel_all.clicked.connect(self._cancel_all)
        self.strat_btn.clicked.connect(self._strategy)
        self.ord_t.doubleClicked.connect(self._cancel_one)
        self._set_enabled(False)

    # ---------- 会话 ----------

    def _set_enabled(self, on: bool) -> None:
        for w in (self.play_btn, self.step_btn, self.strat_btn):
            w.setEnabled(on)

    def load(self) -> None:
        syms = [
            s.strip().upper()
            for s in self.symbols.text().replace("，", ",").split(",")
            if s.strip()
        ]
        if not syms:
            QMessageBox.warning(self, "回放", "请输入标的")
            return
        try:
            self.session = ReplaySession(
                self.sv.history,
                ReplaySettings(
                    day=self.day.date().toPython(),  # type: ignore[arg-type]
                    symbols=syms,
                    start_time=time(self.start.time().hour(), self.start.time().minute()),
                    session=self.sess.currentData(),
                ),
            )
        except ValueError as e:
            QMessageBox.warning(self, "回放", str(e))
            return
        self.pause()
        self.o_sym.clear()
        self.o_sym.addItems(syms)
        self._set_enabled(True)
        for c in self.charts:
            if c.symbol not in syms:
                c.symbol = syms[0]
                c.sym_box.setCurrentText(syms[0])
            c.reload()
        self.session_changed.emit()
        self._refresh()

    def toggle_play(self) -> None:
        if self.playing:
            self.pause()
        else:
            self.playing = True
            self.play_btn.setText("⏸ 暂停")
            self.timer.start()

    def pause(self) -> None:
        self.playing = False
        self.play_btn.setText("▶ 播放")
        self.timer.stop()

    def _tick(self) -> None:
        rs = self.session
        if rs is None:
            return
        rs.advance_by(int(TICK_MS * NS_PER_MS * self.speed.currentData()))
        if rs.finished:
            self.pause()
        for c in self.charts:
            c.tick(follow=True)
        self._refresh()

    def step(self) -> None:
        rs = self.session
        if rs is None:
            return
        self.pause()
        chart = self.charts[0] if self.charts else None
        sym = chart.symbol if chart else rs.st.symbols[0]
        tf = chart.timeframe if chart else "1m"
        rs.step_bar(sym, tf)
        for c in self.charts:
            c.tick(follow=True)
        self._refresh()

    def seek(self) -> None:
        rs = self.session
        if rs is None:
            return
        t = self.seek_time.time()
        target = ny_to_ns(rs.day.day, time(t.hour(), t.minute(), t.second()))
        back = target < rs.now
        if (
            back
            and QMessageBox.question(
                self,
                "向后跳转",
                "向后跳转会开始一个新的练习分支：从空仓开始，策略重新启动。原分支的记录保留。继续吗？",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self.pause()
        rs.seek(target)
        for c in self.charts:
            if back:
                c.reload()
            else:
                c.tick(follow=True)
        self._refresh()

    # ---------- 交易 ----------

    def _order(self, side: str) -> None:
        rs = self.session
        if rs is None:
            return
        otype = self.otype.currentData()
        o = rs.order(
            self.o_sym.currentText(),
            side,
            self.qty.value(),
            otype,
            self.price.value() if otype == "LMT" else None,
        )
        if o.reject_rule:
            QMessageBox.warning(self, "订单被风控拒绝", f"{o.reject_rule}：{o.reject_detail}")
        self._refresh()

    def _flatten(self) -> None:
        if self.session is not None:
            self.session.close_position(self.o_sym.currentText())
            self._refresh()

    def _cancel_all(self) -> None:
        rs = self.session
        if rs is None:
            return
        for o in rs.engine.oms.open_orders():
            rs.cancel(o.client_order_id)
        self._refresh()

    def _cancel_one(self, idx) -> None:  # noqa: ANN001
        row = self.ord_t.row_at(idx)
        if row and self.session is not None:
            self.session.cancel(row["编号"])
            self._refresh()

    def _strategy(self) -> None:
        rs = self.session
        if rs is None:
            return
        if rs.engine.strategy is not None:
            rs.detach_strategy()
            self.strat_btn.setText("挂载策略…")
            self._refresh()
            return
        dlg = StrategyDialog(self.sv, self)
        if dlg.exec() and dlg.form is not None:
            try:
                cls = get_strategy(dlg.box.currentData())
                rs.attach_strategy(cls, cls.Params(**dlg.form.values()))
            except (ValueError, KeyError) as e:
                QMessageBox.warning(self, "挂载失败", str(e))
                return
            self.strat_btn.setText(f"停止策略 {cls.name}")
            for c in self.charts:
                c.reload()
        self._refresh()

    # ---------- 显示 ----------

    def _refresh(self) -> None:
        rs = self.session
        if rs is None:
            return
        self.clock.setText(f"{from_ns(rs.now):%H:%M:%S}")
        self.branch.setText(
            f"{rs.branch.label}（共 {len(rs.branches)} 个）" + ("　已结束" if rs.finished else "")
        )
        eng = rs.engine
        p = eng.portfolio
        pos = [
            {
                "标的": s,
                "数量": x.qty,
                "均价": float(x.avg_cost),
                "最新价": p.last_price.get(s),
                "浮动盈亏": x.unrealized_pnl,
                "已实现": float(x.realized_pnl),
                "所有者": eng.oms.owners.get(s),
            }
            for s, x in p.positions.items()
            if x.qty or x.realized_pnl
        ]
        self.pos_t.set_frame(
            pl.DataFrame(pos) if pos else pl.DataFrame({"说明": ["没有持仓"]}),
            color_col="浮动盈亏" if pos else None,
        )
        orders = [
            {
                "编号": o.client_order_id,
                "标的": o.intent.symbol,
                "方向": o.intent.side,
                "数量": o.intent.qty,
                "类型": o.intent.order_type,
                "限价": float(o.intent.limit_price) if o.intent.limit_price else None,
                "止损价": float(o.intent.stop_price) if o.intent.stop_price else None,
                "状态": o.status.value,
                "已成交": o.filled_qty,
                "来源": o.intent.source,
                "原因": o.intent.reason.code,
            }
            for o in eng.oms.open_orders()
        ]
        self.ord_t.set_frame(
            pl.DataFrame(orders)
            if orders
            else pl.DataFrame({"说明": ["没有挂单（双击挂单可以撤销）"]})
        )
        f = rs.fills()
        if f.height:
            f = f.with_columns(
                pl.col("ts")
                .map_elements(lambda t: f"{from_ns(t):%H:%M:%S}", return_dtype=pl.String)
                .alias("时间")
            )
            self.fill_t.set_frame(
                f,
                [
                    ("时间", "时间"),
                    ("symbol", "标的"),
                    ("side", "方向"),
                    ("qty", "数量"),
                    ("price", "价格"),
                    ("commission", "手续费"),
                    ("reason_code", "原因"),
                ],
            )
        else:
            self.fill_t.set_frame(pl.DataFrame({"说明": ["还没有成交"]}))
        if self.tabs.currentIndex() == 3 or not self.playing:
            rows = []
            for b in rs.branches:
                s = rs.branch_summary(b)
                rows.append(
                    {
                        "分支": s["branch"],
                        "交易笔数": s["trades"],
                        "已平仓净盈亏": s["net_pnl_closed"],
                        "胜率": s["win_rate"],
                        "当前权益": s["equity"],
                        "浮动盈亏": s["unrealized"],
                        "手续费": s["commission"],
                    }
                )
            self.stat_t.set_frame(
                pl.DataFrame(rows),
                formatters={"胜率": lambda v: "-" if v is None else f"{v * 100:.0f}%"},
                color_col="已平仓净盈亏",
            )

    def new_chart(self, config: dict[str, Any] | None = None) -> ReplayChart:
        cfg = dict(config or {})
        if self.session is not None and "symbol" not in cfg:
            cfg["symbol"] = self.session.st.symbols[0]
        c = ReplayChart(self.sv, self, cfg)
        self.charts.append(c)
        c.destroyed.connect(
            lambda _o=None, ch=c: self.charts.remove(ch) if ch in self.charts else None
        )
        return c
