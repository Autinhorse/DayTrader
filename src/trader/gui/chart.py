"""图表窗口：工具栏（标的、周期、时段、指标、跳转、联动）+ Lightweight Charts。

每个图表窗口独立；可以开任意多个，拖出主窗口单独放置。向左拖到头时自动加载更早的数据。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl
from PySide6.QtCore import QDate, QObject, Qt, QUrl, Signal, Slot
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from trader.core.trading_calendar import TradingDay
from trader.gui.services import TIMEFRAMES, IndicatorSpec, Services, ny_seconds, today

ASSETS = Path(__file__).resolve().parent / "assets"


class _Bridge(QObject):
    ready_signal = Signal()
    more_signal = Signal()

    @Slot()
    def ready(self) -> None:
        self.ready_signal.emit()

    @Slot()
    def needMore(self) -> None:  # noqa: N802  JS 端调用的名字
        self.more_signal.emit()


class ChartView(QWebEngineView):
    """加载 chart.html 的网页视图；页面就绪前的调用先排队。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.bridge = _Bridge()
        self.channel = QWebChannel(self.page())
        self.channel.registerObject("bridge", self.bridge)
        self.page().setWebChannel(self.channel)
        s = self.settings()
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        self._ready = False
        self._pending: list[str] = []
        self.bridge.ready_signal.connect(self._on_ready)
        self.page().setBackgroundColor(Qt.GlobalColor.black)
        self.setUrl(QUrl.fromLocalFile(str(ASSETS / "chart.html")))

    def _on_ready(self) -> None:
        self._ready = True
        for js in self._pending:
            self.page().runJavaScript(js)
        self._pending.clear()

    def call(self, fn: str, *args: Any) -> None:
        js = f"window.api.{fn}({', '.join(json.dumps(a, ensure_ascii=False) for a in args)});"
        if self._ready:
            self.page().runJavaScript(js)
        else:
            self._pending.append(js)


class ChartWidget(QWidget):
    """一个图表窗口。config() / apply_config() 用于保存和恢复布局。"""

    title_changed = Signal(str)

    def __init__(self, services: Services, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.sv = services
        cfg = config or {}
        symbols = services.symbols()
        self.symbol = cfg.get("symbol") or (symbols[0] if symbols else "SPY")
        self.timeframe = cfg.get("timeframe", "1m")
        self.session = cfg.get("session", "rth")
        self.indicators = [IndicatorSpec(**x) for x in cfg.get("indicators", [])]
        self.days: list[TradingDay] = []
        self.overlay_fills: pl.DataFrame | None = None  # 联动显示的回测成交
        self.overlay_label = ""

        self.sym_box = QComboBox()
        self.sym_box.setEditable(True)
        self.sym_box.addItems(symbols)
        self.sym_box.setCurrentText(self.symbol)
        self.tf_box = QComboBox()
        self.tf_box.addItems(TIMEFRAMES)
        self.tf_box.setCurrentText(self.timeframe)
        self.sess_box = QComboBox()
        self.sess_box.addItem("常规时段", "rth")
        self.sess_box.addItem("含盘前盘后", "extended")
        self.sess_box.setCurrentIndex(0 if self.session == "rth" else 1)
        self.date_box = QDateEdit()
        self.date_box.setCalendarPopup(True)
        self.date_box.setDisplayFormat("yyyy-MM-dd")
        self.date_box.setDate(QDate.currentDate())
        go = QPushButton("跳到")
        ind_btn = QPushButton("指标…")
        self.link = QCheckBox("联动")
        self.link.setToolTip("勾选后，在回测结果里点击交易时这个图表会跳过去")
        self.link.setChecked(cfg.get("linked", True))
        clear_btn = QPushButton("清除标记")
        bar = QHBoxLayout()
        for w in (
            self.sym_box,
            self.tf_box,
            self.sess_box,
            self.date_box,
            go,
            ind_btn,
            self.link,
            clear_btn,
        ):
            bar.addWidget(w)
        bar.addStretch()
        self.view = ChartView()
        self.view.setMinimumHeight(260)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.addLayout(bar)
        lay.addWidget(self.view, 1)
        self.status = QLabel("")
        self.status.setStyleSheet("color: gray")
        lay.addWidget(self.status)

        self.sym_box.activated.connect(lambda _i: self._changed())
        le = self.sym_box.lineEdit()
        if le is not None:
            le.returnPressed.connect(self._changed)
        self.tf_box.activated.connect(lambda _i: self._changed())
        self.sess_box.activated.connect(lambda _i: self._changed())
        go.clicked.connect(lambda: self.load(self.date_box.date().toPython()))  # type: ignore[arg-type]
        ind_btn.clicked.connect(self._edit_indicators)
        clear_btn.clicked.connect(self.clear_overlay)
        self.view.bridge.more_signal.connect(self.load_more)
        self.load(None)

    # ---------- 配置 ----------

    def config(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "session": self.session,
            "indicators": [
                {"name": i.name, "params": i.params, "color": i.color} for i in self.indicators
            ],
            "linked": self.link.isChecked(),
        }

    def title(self) -> str:
        return f"{self.symbol} {self.timeframe}"

    def _changed(self) -> None:
        self.symbol = self.sym_box.currentText().strip().upper()
        self.timeframe = self.tf_box.currentText()
        self.session = self.sess_box.currentData()
        self.title_changed.emit(self.title())
        end = self.days[-1].day if self.days else None
        self.load(end)

    # ---------- 加载 ----------

    def load(
        self, end_day: date | None, n_days: int | None = None, focus: tuple[int, int] | None = None
    ) -> None:
        if end_day is None:
            cov = self.sv.coverage(self.symbol)
            end_day = cov[1] if cov else today()
        self.days = self.sv.window_days(self.symbol, self.timeframe, end_day, n_days)
        self._render(prepended=0, focus=focus)

    def load_more(self) -> None:
        """向左拖到头：再往前加载一段。"""
        if not self.days:
            return
        first = self.days[0].day
        more = self.sv.window_days(
            self.symbol, self.timeframe, self.sv.cal.previous_trading_day(first)
        )
        more = [d for d in more if d.day < first]
        if not more:
            self.view.call("noMore")
            return
        before = self.sv.history.bars(
            self.symbol, self.timeframe, more[0].start, more[-1].end, self.session
        ).height
        self.days = more + self.days
        self._render(prepended=before)

    def _render(self, prepended: int, focus: tuple[int, int] | None = None) -> None:
        p = self.sv.chart_payload(
            self.symbol, self.timeframe, self.session, self.days, self.indicators
        )  # type: ignore[arg-type]
        markers = []
        if self.overlay_fills is not None and p["candles"]:
            lo, hi = p["candles"][0]["time"], p["candles"][-1]["time"]
            # 只标出已加载范围内的成交，否则图表会把范围外的标记挤到边上
            markers = [
                m
                for m in self.sv.trade_markers(self.overlay_fills, self.symbol)
                if lo <= m["time"] <= hi
            ]
        p["markers"] = markers
        p["prepended"] = prepended
        sess = "常规时段" if self.session == "rth" else "含盘前盘后"
        title = f"{self.symbol} · {self.timeframe} · {sess}"
        if self.overlay_label:
            title += f" · {self.overlay_label}"
        p["title"] = title
        if focus is not None:
            p["range"] = [ny_seconds(focus[0]), ny_seconds(focus[1])]
        self.view.call("setData", p)
        if self.days:
            self.status.setText(
                f"{self.days[0].day} ~ {self.days[-1].day}，{len(p['candles']):,} 根；"
                "时间为纽约时间"
            )
        else:
            self.status.setText("没有数据")

    # ---------- 回测联动 ----------

    def show_trade(
        self, fills: pl.DataFrame, label: str, symbol: str, entry: int, exit_: int | None
    ) -> None:
        """跳到一笔交易：切换到该标的，加载覆盖这笔交易的数据，标出这次回测的全部买卖点。"""
        self.overlay_fills, self.overlay_label = fills, label
        if symbol != self.symbol:
            self.symbol = symbol
            self.sym_box.setCurrentText(symbol)
            self.title_changed.emit(self.title())
        end = exit_ or entry
        day = self.sv.cal.trading_date_of(end) or today()
        span = max(end - entry, 0)
        pad = max(span // 2, 20 * 60 * 10**9)
        need_first = self.sv.cal.trading_date_of(entry - pad) or day
        n = len(self.sv.cal.trading_days(need_first, day))
        self.load(
            day,
            max(n, 1) + (1 if self.timeframe not in ("1s", "5s") else 0),
            focus=(entry - pad, end + pad),
        )

    def clear_overlay(self) -> None:
        self.overlay_fills, self.overlay_label = None, ""
        self._render(prepended=0)

    # ---------- 指标 ----------

    def _edit_indicators(self) -> None:
        from trader.gui.indicator_dialog import IndicatorDialog

        dlg = IndicatorDialog(self.sv, self.indicators, self)
        if dlg.exec():
            self.indicators = dlg.result_specs()
            self._render(prepended=0)
