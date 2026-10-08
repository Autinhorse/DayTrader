"""实时运行时的历史查询（DESIGN.md 5.5）：本地 Massive 数据 + 当天的 IB 补数和实时 1 秒 bar。

当天的数据分三段，按时间先后：
1. [交易日开始, fine_from)：IB 补数的 1 分钟 bar（一次请求就能取整天；1 秒 bar 补数太慢，IB 限速）；
2. [fine_from, handoff)：IB 补数的 1 秒 bar（最近约 30 分钟）；
3. [handoff, …)：实时 1 秒 bar（逐笔或快照生成）。
1 分钟及以上周期由三段合起来聚合（1 分钟 bar 落在对应桶里，结果与 1 秒聚合相同）；
1 分钟以下的周期只能用 1 秒数据，所以只有 fine_from 之后才有。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from trader.core.models import Bar
from trader.core.timeutil import NS_PER_MIN, timeframe_ns
from trader.core.trading_calendar import SessionFilter, TradingCalendar, TradingDay
from trader.data.aggregate import aggregate_day, label_1s
from trader.data.catalog import Catalog
from trader.data.history import HistoryService, empty_frame
from trader.data.store import BAR_SCHEMA, empty_bars


class LiveHistory(HistoryService):
    def __init__(self, data_dir: Path, cal: TradingCalendar, catalog: Catalog, today: date) -> None:
        super().__init__(data_dir, cal, catalog)
        self.today = today
        self._minutes: dict[str, pl.DataFrame] = {}
        self._seconds: dict[str, list[tuple]] = {}
        self.fine_from: dict[str, int] = {}

    def set_backfill(
        self, symbol: str, minutes: pl.DataFrame, seconds: pl.DataFrame, fine_from: int
    ) -> None:
        """minutes / seconds 是 BAR_SCHEMA 的 1 分钟、1 秒 bar（ts_start 已过滤好）。"""
        self._minutes[symbol] = minutes.filter(pl.col("ts_start") < fine_from)
        rows = seconds.filter(pl.col("ts_start") >= fine_from).select(list(BAR_SCHEMA)).rows()
        self._seconds[symbol] = list(rows) + [
            r for r in self._seconds.get(symbol, []) if r[0] >= fine_from
        ]
        self._seconds[symbol].sort(key=lambda r: r[0])
        self.fine_from[symbol] = fine_from

    def append(self, bar: Bar) -> None:
        """实时 1 秒 bar（按时间顺序到达）。"""
        self._seconds.setdefault(bar.symbol, []).append(
            (bar.ts_start, bar.open, bar.high, bar.low, bar.close, bar.volume, bar.vwap, bar.trades)
        )

    def _fine(self, symbol: str) -> pl.DataFrame:
        rows = self._seconds.get(symbol)
        if not rows:
            return empty_bars()
        return pl.DataFrame(rows, schema=BAR_SCHEMA, orient="row")

    def _coarse_and_fine(self, symbol: str) -> pl.DataFrame:
        parts = [df for df in (self._minutes.get(symbol), self._fine(symbol)) if df is not None]
        parts = [p for p in parts if not p.is_empty()]
        return pl.concat(parts).sort("ts_start") if parts else empty_bars()

    def day_bars(
        self, symbol: str, timeframe: str, day: TradingDay, session: SessionFilter
    ) -> pl.DataFrame:
        if day.day != self.today:
            return super().day_bars(symbol, timeframe, day, session)
        if timeframe == "1s":
            fine = self._fine(symbol)
            return label_1s(fine, day, session) if not fine.is_empty() else empty_frame()
        src = (
            self._fine(symbol)
            if timeframe_ns(timeframe) < NS_PER_MIN
            else self._coarse_and_fine(symbol)
        )
        if src.is_empty():
            return empty_frame()
        return aggregate_day(src, day, timeframe, session)

    def seed_bars(self, symbol: str, start: int, end: int, session: SessionFilter) -> pl.DataFrame:
        day = self.cal.trading_day(self.today) if self.cal.is_trading_day(self.today) else None
        if day is None or not (day.start <= start < day.end):
            return super().seed_bars(symbol, start, end, session)
        df = label_1s(self._coarse_and_fine(symbol), day, session)
        return df.filter((pl.col("ts_start") >= start) & (pl.col("ts_start") < end))
