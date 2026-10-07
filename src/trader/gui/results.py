"""回测结果（DESIGN.md 10.7 第 2～4 条）：汇总统计、成本分项与成本敏感性、权益曲线与每日盈亏、
分组统计、路径歧义和数据质量提示；交易明细表可排序和筛选，点击一笔交易让联动的图表跳过去复盘。"""

from __future__ import annotations

import json
from typing import Any

import polars as pl
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from trader.backtest.experiments import KEY_METRICS, LoadedRun, format_metric, metric
from trader.core.timeutil import from_ns
from trader.gui.chart import ChartView
from trader.gui.tables import FrameTable


def ts_text(ts: int | None) -> str:
    return "-" if ts is None else f"{from_ns(ts):%Y-%m-%d %H:%M:%S}"


def half_hour(ts: int) -> str:
    t = from_ns(ts)
    return f"{t.hour:02d}:{'00' if t.minute < 30 else '30'}"


def _kv_table(rows: list[tuple[str, str]]) -> QTableWidget:
    t = QTableWidget(len(rows), 2)
    t.setHorizontalHeaderLabels(["项目", "数值"])
    t.verticalHeader().setVisible(False)
    for i, (k, v) in enumerate(rows):
        t.setItem(i, 0, QTableWidgetItem(k))
        it = QTableWidgetItem(v)
        it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        t.setItem(i, 1, it)
    t.resizeColumnsToContents()
    return t


TRADE_COLUMNS = [
    ("symbol", "标的"),
    ("direction", "方向"),
    ("qty", "数量"),
    ("entry_time", "入场时间"),
    ("entry_price", "入场价"),
    ("entry_reason", "入场原因"),
    ("exit_time", "出场时间"),
    ("exit_price", "出场价"),
    ("exit_reason", "出场原因"),
    ("net_pnl", "净盈亏"),
    ("gross_pnl", "毛盈亏"),
    ("commission", "手续费"),
    ("holding_min", "持仓(分)"),
    ("mfe", "最大浮盈"),
    ("mae", "最大浮亏"),
    ("ambiguous", "路径歧义"),
    ("out_of_range", "超出流动性"),
]


class ResultsPanel(QWidget):
    trade_selected = Signal(object, dict)  # (LoadedRun, 交易行)

    def __init__(self) -> None:
        super().__init__()
        self.run: LoadedRun | None = None
        self.header = QLabel("还没有打开回测结果：运行一次回测，或在实验列表里双击一条记录。")
        self.header.setWordWrap(True)
        self.warn = QLabel("")
        self.warn.setWordWrap(True)
        self.warn.setStyleSheet("color: #e6a23c")
        self.tabs = QTabWidget()

        self.summary_holder = QHBoxLayout()
        summary_w = QWidget()
        summary_w.setLayout(self.summary_holder)
        self.tabs.addTab(summary_w, "概要")

        self.equity = ChartView()
        self.tabs.addTab(self.equity, "权益与每日盈亏")

        groups_w = QWidget()
        g = QVBoxLayout(groups_w)
        self.group_box = QComboBox()
        for key, label in (
            ("by_entry_reason", "按入场原因"),
            ("by_exit_reason", "按出场原因"),
            ("by_direction", "按多空"),
            ("by_day", "按交易日"),
            ("by_half_hour", "按入场时间段（30 分钟）"),
        ):
            self.group_box.addItem(label, key)
        self.group_table = FrameTable()
        g.addWidget(self.group_box)
        g.addWidget(self.group_table)
        self.tabs.addTab(groups_w, "分组统计")

        trades_w = QWidget()
        tl = QVBoxLayout(trades_w)
        frow = QHBoxLayout()
        self.f_entry = QComboBox()
        self.f_exit = QComboBox()
        self.f_dir = QComboBox()
        self.f_pnl = QComboBox()
        self.f_slot = QComboBox()
        for w, label in (
            (self.f_entry, "入场原因"),
            (self.f_exit, "出场原因"),
            (self.f_dir, "方向"),
            (self.f_pnl, "盈亏"),
            (self.f_slot, "时间段"),
        ):
            frow.addWidget(QLabel(label))
            frow.addWidget(w)
            w.currentIndexChanged.connect(self._apply_filter)
        frow.addStretch()
        self.count = QLabel("")
        frow.addWidget(self.count)
        self.trades = FrameTable()
        self.detail = QTextBrowser()
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.trades)
        split.addWidget(self.detail)
        split.setSizes([400, 140])
        tl.addLayout(frow)
        tl.addWidget(split)
        self.tabs.addTab(trades_w, "交易明细")

        self.orders = FrameTable()
        self.tabs.addTab(self.orders, "订单")

        lay = QVBoxLayout(self)
        lay.addWidget(self.header)
        lay.addWidget(self.warn)
        lay.addWidget(self.tabs, 1)

        self.group_box.currentIndexChanged.connect(self._show_group)
        self.trades.clicked.connect(self._trade_clicked)

    # ---------- 显示一次回测 ----------

    def show_run(self, run: LoadedRun) -> None:
        self.run = run
        c, s = run.config, run.summary
        params = ", ".join(f"{k}={v}" for k, v in c.get("params", {}).items())
        self.header.setText(
            f"<b>{c.get('name') or c['strategy']}</b>　{c['strategy']}({params})　"
            f"{c['start']} ~ {c['end']}　时段 {c.get('session')}　"
            f"<span style='color:gray'>{run.run_id}</span>"
        )
        warns = []
        pa, lq = s.get("path_ambiguity", {}), s.get("liquidity", {})
        if pa.get("trades"):
            warns.append(
                f"路径歧义 {pa['trades']} 笔：若按有利结果，"
                f"盈亏多 {pa.get('pnl_if_favorable', 0):,.2f}"
            )
        if lq.get("out_of_range_trades"):
            warns.append(
                f"{lq['out_of_range_trades']} 笔（{lq.get('out_of_range_pct', 0):.0f}%）"
                "超出市价单模型适用范围，成交假设不可靠"
            )
        gaps = {
            k: v for k, v in s.get("data_quality", {}).get("days_with_rth_gaps", {}).items() if v
        }
        if gaps:
            warns.append(
                "数据有长空档的交易日：" + "，".join(f"{k} {v} 天" for k, v in gaps.items())
            )
        if s.get("counts", {}).get("open_at_end"):
            warns.append(f"回测结束时仍有 {s['counts']['open_at_end']} 笔未平仓")
        self.warn.setText("　｜　".join(warns))
        self._fill_summary()
        self._fill_equity()
        self._show_group()
        self._fill_trades()
        self.orders.set_frame(
            run.orders.with_columns(
                pl.col("created_at").map_elements(ts_text, return_dtype=pl.String).alias("time")
            ),
            [
                ("time", "时间"),
                ("symbol", "标的"),
                ("side", "方向"),
                ("qty", "数量"),
                ("order_type", "类型"),
                ("role", "角色"),
                ("status", "状态"),
                ("filled_qty", "成交量"),
                ("avg_fill_price", "成交均价"),
                ("commission", "手续费"),
                ("reason_code", "原因"),
                ("reject_rule", "拒绝规则"),
                ("reject_detail", "拒绝说明"),
            ],
        )

    def _fill_summary(self) -> None:
        assert self.run is not None
        s = self.run.summary
        while self.summary_holder.count():
            item = self.summary_holder.takeAt(0)
            w = item.widget() if item is not None else None
            if w is not None:
                w.setParent(None)  # type: ignore[call-overload]
        rows = [(name, format_metric(metric(s, path), f)) for name, path, f in KEY_METRICS]
        self.summary_holder.addWidget(_kv_table(rows), 1)
        pnl = s.get("pnl", {})
        cost_rows = [
            ("毛盈亏（按含成本的成交价）", f"{pnl.get('gross_pnl', 0):,.2f}"),
            ("其中：价差成本", f"{pnl.get('spread_cost', 0):,.2f}"),
            ("其中：冲击成本", f"{pnl.get('impact_cost', 0):,.2f}"),
            ("手续费", f"{pnl.get('commission', 0):,.2f}"),
            ("净盈亏", f"{pnl.get('net_pnl', 0):,.2f}"),
            ("", ""),
        ]
        cs = s.get("cost_sensitivity", {})
        for sc in cs.get("scenarios", []):
            pf = sc.get("profit_factor")
            cost_rows.append(
                (
                    f"成本 × {sc['multiplier']:g}",
                    f"净 {sc['net_pnl']:,.2f}　利润因子 {'-' if pf is None else f'{pf:.2f}'}",
                )
            )
        be = cs.get("breakeven_extra_cost_per_share")
        cost_rows.append(("盈亏平衡：每股再多付", "-" if be is None else f"{be:.4f} 美元"))
        self.summary_holder.addWidget(_kv_table(cost_rows), 1)

    def _fill_equity(self) -> None:
        assert self.run is not None
        days = daily_equity(self.run)
        daily = [
            {
                "time": d["day"],
                "value": d["pnl"],
                "color": "#26a69a" if d["pnl"] >= 0 else "#ef5350",
            }
            for d in self.run.summary.get("daily_pnl", [])
        ]
        self.equity.call(
            "setLines",
            {
                "title": "收盘权益变化（相对初始资金，每个交易日一个点）与每日盈亏",
                "series": [
                    {"name": "权益变化", "data": days, "color": "#2962ff"},
                    {"name": "每日盈亏", "data": daily, "kind": "histogram", "pane": 1},
                ],
            },
        )

    def _show_group(self) -> None:
        if self.run is None:
            return
        key = self.group_box.currentData()
        rows = self.run.summary.get("groups", {}).get(key, [])
        df = pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()
        if df.is_empty():
            self.group_table.set_frame(pl.DataFrame({"说明": ["没有交易"]}))
            return
        name_col = df.columns[0]
        self.group_table.set_frame(
            df,
            [
                (name_col, "分组"),
                ("trades", "交易笔数"),
                ("net_pnl", "净盈亏"),
                ("win_rate", "胜率"),
            ],
            {"win_rate": lambda v: "-" if v is None else f"{v * 100:.1f}%"},
            color_col="net_pnl",
        )

    def _fill_trades(self) -> None:
        assert self.run is not None
        t = self.run.trades.with_columns(
            (pl.col("holding_s") / 60).alias("holding_min"),
            pl.col("entry_time").map_elements(half_hour, return_dtype=pl.String).alias("slot"),
        )
        self._trades_df = t
        self.trades.set_frame(
            t,
            TRADE_COLUMNS,
            {"entry_time": ts_text, "exit_time": ts_text},
            color_col="net_pnl",
        )
        for box, col in (
            (self.f_entry, "entry_reason"),
            (self.f_exit, "exit_reason"),
            (self.f_dir, "direction"),
            (self.f_slot, "slot"),
        ):
            box.blockSignals(True)
            box.clear()
            box.addItem("全部", None)
            for v in sorted({x for x in t[col].to_list() if x is not None}):
                box.addItem(str(v), v)
            box.blockSignals(False)
        self.f_pnl.blockSignals(True)
        self.f_pnl.clear()
        for label, v in (("全部", None), ("盈利", "win"), ("亏损", "loss")):
            self.f_pnl.addItem(label, v)
        self.f_pnl.blockSignals(False)
        self._apply_filter()

    def _apply_filter(self) -> None:
        conds = {
            "entry_reason": self.f_entry.currentData(),
            "exit_reason": self.f_exit.currentData(),
            "direction": self.f_dir.currentData(),
            "slot": self.f_slot.currentData(),
        }
        pnl = self.f_pnl.currentData()

        def ok(r: dict[str, Any]) -> bool:
            if any(v is not None and r.get(k) != v for k, v in conds.items()):
                return False
            if pnl == "win" and not (r.get("net_pnl") or 0) > 0:
                return False
            return not (pnl == "loss" and (r.get("net_pnl") or 0) > 0)

        self.trades.set_filter(ok)
        self.count.setText(f"显示 {self.trades.visible_count()} 笔")

    def _trade_clicked(self, index) -> None:  # noqa: ANN001
        row = self.trades.row_at(index)
        if row is None or self.run is None:
            return
        ctx = self._entry_context(row)
        side = "做多" if row["direction"] == "long" else "做空"
        lines = [
            f"<b>{row['symbol']} {side} {row['qty']} 股</b>",
            f"入场 {ts_text(row['entry_time'])} @ {row['entry_price']:.4f}　"
            f"原因 <b>{row['entry_reason']}</b>",
            f"出场 {ts_text(row['exit_time'])} @ {(row['exit_price'] or 0):.4f}　"
            f"原因 <b>{row['exit_reason']}</b>",
            f"净盈亏 {row['net_pnl']:,.2f}（毛 {row['gross_pnl']:,.2f}，"
            f"手续费 {row['commission']:,.2f}）　持仓 {row['holding_min'] or 0:.1f} 分钟　"
            f"最大浮盈 {row['mfe'] or 0:,.2f}　最大浮亏 {row['mae'] or 0:,.2f}",
        ]
        if ctx:
            lines.append(
                "入场时的指标值："
                + "，".join(
                    f"{k} = {v:.4f}" if isinstance(v, (int, float)) else f"{k} = {v}"
                    for k, v in ctx.items()
                )
            )
        if row.get("ambiguous"):
            lines.append(
                f"<span style='color:#e6a23c'>⚠ 路径歧义：同一秒内止盈止损都可触及，按止损计算；"
                f"若按止盈，盈亏多 {row.get('ambiguous_alt_gain', 0):,.2f}</span>"
            )
        if row.get("out_of_range"):
            lines.append(
                "<span style='color:#e6a23c'>⚠ 订单规模超出市价单模型适用范围，"
                "成交价可能偏乐观</span>"
            )
        self.detail.setHtml("<br>".join(lines))
        self.trade_selected.emit(self.run, row)

    def _entry_context(self, row: dict[str, Any]) -> dict[str, Any]:
        assert self.run is not None
        f = self.run.fills.filter(
            (pl.col("ts") == row["entry_time"]) & (pl.col("reason_code") == row["entry_reason"])
        )
        if f.is_empty():
            return {}
        coid = f["client_order_id"][0]
        o = self.run.orders.filter(pl.col("client_order_id") == coid)
        if o.is_empty():
            return {}
        try:
            return json.loads(o["reason_context"][0] or "{}")
        except ValueError:
            return {}


def daily_equity(run: LoadedRun) -> list[dict[str, Any]]:
    """每个交易日收盘后的权益变化（相对初始资金），时间用 "YYYY-MM-DD"。"""
    init = float(run.config.get("initial_cash", 100000))
    out: dict[str, float] = {}
    total = 0.0
    for d in run.summary.get("daily_pnl", []):
        total += d["pnl"]
        out[d["day"]] = total
    if not out and run.equity.height:  # 旧结果没有 daily_pnl：用最后一个权益点
        out["end"] = float(run.equity["equity"][-1]) - init
    return [{"time": k, "value": v} for k, v in sorted(out.items())]
