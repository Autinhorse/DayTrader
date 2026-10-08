"""界面使用的后端服务：数据、指标、策略、实验记录。界面里所有面板共用一份。

研究版界面直接调用这些模块（决策 0005），不经过网络服务。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from trader.config import load_universe
from trader.core.aggregation import SUPPORTED_TIMEFRAMES
from trader.core.clock import WallClock
from trader.core.timeutil import NS_PER_SEC, from_ns, ny_date
from trader.core.trading_calendar import SessionFilter, TradingCalendar, TradingDay
from trader.data.catalog import Catalog
from trader.data.history import HistoryService
from trader.data.indicator_service import IndicatorService
from trader.indicators.base import get_indicator, list_indicators, load_all
from trader.strategy.base import list_strategies, load_user_strategies

TIMEFRAMES = list(SUPPORTED_TIMEFRAMES)

# 每次加载的交易日数（向左拖动时再按同样的天数往前加载）
DAYS_PER_LOAD = {
    "1s": 1,
    "5s": 1,
    "10s": 2,
    "30s": 3,
    "1m": 5,
    "2m": 8,
    "5m": 15,
    "15m": 30,
    "30m": 60,
    "1h": 90,
    "1d": 400,
}


def today() -> date:
    """纽约的今天（经由 Clock 取时间，不直接读系统时间）。"""
    return ny_date(WallClock(lambda _e: None).now())


def ny_seconds(ts_ns: int) -> int:
    """UTC 纳秒 → 图表用的时间：纽约当地时间的“秒数”（把当地时间当作 UTC 显示）。"""
    dt = from_ns(ts_ns)
    off = dt.utcoffset()
    return ts_ns // NS_PER_SEC + (int(off.total_seconds()) if off else 0)


@dataclass
class IndicatorSpec:
    name: str
    params: dict[str, Any]
    color: str | None = None

    @property
    def label(self) -> str:
        p = ",".join(str(v) for v in self.params.values())
        return f"{self.name}({p})" if p else self.name


class Services:
    def __init__(self, project_dir: Path, data_dir: Path) -> None:
        self.project_dir = project_dir
        self.data_dir = data_dir
        self.cal = TradingCalendar()
        self.catalog = Catalog(data_dir / "catalog.sqlite")
        self.history = HistoryService(data_dir, self.cal, self.catalog)
        self.indicators = IndicatorService(self.history)
        self.runs_dir = project_dir / "runs"
        self.load_errors: dict[str, str] = {}
        self.reload_user_code()

    # ---------- 用户代码 ----------

    def reload_user_code(self) -> dict[str, str]:
        """重新加载 user/indicators 和 user/strategies（研究版支持保存后热加载）。"""
        errs = load_all(self.project_dir / "user" / "indicators")
        errs |= {
            f"strategies/{k}": v
            for k, v in load_user_strategies(self.project_dir / "user" / "strategies").items()
        }
        self.load_errors = errs
        return errs

    def indicator_classes(self):
        return list_indicators()

    def strategy_classes(self):
        return list_strategies()

    # ---------- 标的与覆盖 ----------

    def symbols(self) -> list[str]:
        names = load_universe(self.project_dir / "config" / "universe.yaml").names
        extra = [s for s in self.catalog.symbols() if s not in names]
        return names + extra

    def coverage(self, symbol: str) -> tuple[date, date] | None:
        days = self.history.coverage(symbol)
        return (days[0], days[-1]) if days else None

    # ---------- 图表数据 ----------

    def window_days(
        self, symbol: str, timeframe: str, end_day: date, n_days: int | None = None
    ) -> list[TradingDay]:
        """截至 end_day（含）往前 n 个有数据的交易日。"""
        n = n_days or DAYS_PER_LOAD.get(timeframe, 5)
        cov = self.coverage(symbol)
        if cov is None:
            return []
        end_day = min(end_day, cov[1])
        start = end_day - timedelta(days=int(n * 1.6) + 7)
        days = [d for d in self.cal.trading_days(max(start, cov[0]), end_day)]
        return days[-n:]

    def chart_payload(
        self,
        symbol: str,
        timeframe: str,
        session: SessionFilter,
        days: list[TradingDay],
        indicators: list[IndicatorSpec],
    ) -> dict[str, Any]:
        if not days:
            return {"candles": [], "volume": [], "indicators": []}
        start, end = days[0].start, days[-1].end
        bars = self.history.bars(symbol, timeframe, start, end, session)
        times = [ny_seconds(t) for t in bars["ts_start"].to_list()]
        o, h, lo, c, v = (bars[k].to_list() for k in ("open", "high", "low", "close", "volume"))
        candles = [
            {"time": t, "open": a, "high": b, "low": x, "close": y}
            for t, a, b, x, y in zip(times, o, h, lo, c, strict=True)
        ]
        volume = [
            {"time": t, "value": vol, "color": "#26a69a55" if y >= a else "#ef535055"}
            for t, vol, a, y in zip(times, v, o, c, strict=True)
        ]
        inds = []
        for spec in indicators:
            try:
                df = self.indicators.compute(
                    symbol, timeframe, spec.name, spec.params, start, end, session
                )
            except Exception as e:  # 指标出错不影响图表其他部分
                inds.append({"label": f"{spec.label} 出错：{e}", "outputs": []})
                continue
            cls = get_indicator(spec.name)
            t = [ny_seconds(x) for x in df["ts_start"].to_list()]
            outs = []
            for i, o_spec in enumerate(cls.outputs):
                vals = df[o_spec.key].to_list()
                data = [
                    {"time": tt, "value": vv}
                    for tt, vv in zip(t, vals, strict=True)
                    if vv is not None
                ]
                if cls.scope == "session":
                    break_between_days(data, step=o_spec.plot == "hline")
                color = (spec.color if i == 0 and spec.color else None) or o_spec.color
                outs.append(
                    {
                        "key": o_spec.key,
                        "plot": o_spec.plot,
                        "pane": o_spec.pane,
                        "color": color,
                        "data": data,
                    }
                )
            inds.append({"label": spec.label, "outputs": outs})
        last = c[-1] if c else 1.0
        return {
            "candles": candles,
            "volume": volume,
            "indicators": inds,
            "precision": 2 if last >= 1 else 4,
            "minMove": 0.01 if last >= 1 else 0.0001,
        }

    def trade_markers(self, fills: pl.DataFrame, symbol: str) -> list[dict[str, Any]]:
        """回测成交 → 图上的买卖箭头（悬停时显示数量、价格、原因）。"""
        out = []
        f = fills.filter(pl.col("symbol") == symbol)
        for r in f.iter_rows(named=True):
            buy = r["side"] == "BUY"
            out.append(
                {
                    "time": ny_seconds(r["ts"]),
                    "position": "belowBar" if buy else "aboveBar",
                    "color": "#26a69a" if buy else "#ef5350",
                    "shape": "arrowUp" if buy else "arrowDown",
                    "text": f"{'买' if buy else '卖'}{r['qty']}@{r['price']:.2f} {r['reason_code']}"
                    + (" ⚠歧义" if r.get("ambiguous") else ""),
                }
            )
        return out


def break_between_days(
    data: list[dict[str, Any]], step: bool = False, gap_s: int = 3 * 3600
) -> None:
    """会话指标每天重新开始：跨夜的那一段线画成透明，避免看起来像价格跳变。

    普通线的线段颜色取自起点，所以把前一天最后一个点设为透明；阶梯线的竖段颜色取自终点。
    """
    for prev, cur in zip(data, data[1:], strict=False):
        if cur["time"] - prev["time"] > gap_s:
            (cur if step else prev)["color"] = "rgba(0,0,0,0)"
