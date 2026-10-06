"""历史查询服务（DESIGN.md 5.6）。阶段 1 只提供 1 秒 bar，其他周期在阶段 2 由聚合器提供。

不直接暴露给策略：策略的 ctx.history 另行实现，只返回引擎当前时间之前已可用的数据。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

import polars as pl

from trader.core.timeutil import ny_date
from trader.core.trading_calendar import TradingCalendar
from trader.data.catalog import Catalog
from trader.data.corporate_actions import adjust_for_splits, load_actions, split_factor
from trader.data.store import BarStore, empty_bars

SessionFilter = Literal["rth", "extended"]


class HistoryService:
    def __init__(self, data_dir: Path, cal: TradingCalendar, catalog: Catalog) -> None:
        self.data_dir = Path(data_dir)
        self.store = BarStore(data_dir)
        self.cal = cal
        self.catalog = catalog

    def bars(
        self,
        symbol: str,
        timeframe: str,
        start: int,
        end: int,
        session: SessionFilter = "rth",
        adjusted: bool = False,
    ) -> pl.DataFrame:
        """[start, end) 内的 bar（UTC 纳秒）。rth 只含常规时段，extended 含盘前盘后。

        adjusted=True 时按拆股复权到 end 所在日期的股本口径；默认返回原始价格。
        """
        if timeframe != "1s":
            raise NotImplementedError("阶段 1 只提供 1 秒 bar；其他周期在阶段 2 由聚合器提供")
        if end <= start:
            return empty_bars()
        actions = load_actions(self.data_dir) if adjusted else None
        as_of = ny_date(end - 1)
        frames = []
        for day in self.cal.trading_days(ny_date(start), as_of):
            df = self.store.read_day(symbol, day.day)
            if df.is_empty():
                continue
            lo, hi = (day.open, day.close) if session == "rth" else (day.pre_open, day.post_close)
            lo, hi = max(lo, start), min(hi, end)
            df = df.filter((pl.col("ts_start") >= lo) & (pl.col("ts_start") < hi))
            if actions is not None:
                df = adjust_for_splits(df, split_factor(actions, symbol, day.day, as_of))
            frames.append(df)
        return pl.concat(frames) if frames else empty_bars()

    def coverage(self, symbol: str) -> list[date]:
        """有数据的日期。"""
        return [p.day for p in self.catalog.partitions(symbol) if p.rows > 0]
