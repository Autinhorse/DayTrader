"""指标计算服务（DESIGN.md 6.5）的历史计算部分：每次调用新建指标实例，按预热需求取数，
返回对齐到 bar 时间戳的表格，供图表和 notebook 使用。不读也不写引擎里的活动实例。

预热起点取三项要求中最早的：固定根数、当前交易日开始、之前 N 个完整交易日。
因此从盘中任意时刻开始计算，日内 VWAP 仍从当天开盘算起，前一日高低收仍取自上一个完整交易日，
与从更早时刻开始计算在同一时刻的数值相同。
"""

from __future__ import annotations

from typing import Any

import polars as pl

from trader.core.models import Bar
from trader.core.trading_calendar import SessionFilter, TradingDay
from trader.data.history import HistoryService
from trader.indicators.base import Indicator, create

# 为凑够预热根数最多往前找这么多个交易日
MAX_LOOKBACK_DAYS = 400


class IndicatorService:
    def __init__(self, history: HistoryService) -> None:
        self.history = history

    def compute(
        self,
        symbol: str,
        timeframe: str,
        name: str,
        params: dict[str, Any],
        start: int,
        end: int,
        session: SessionFilter = "extended",
    ) -> pl.DataFrame:
        """返回列 ts_start, ts_end, <各输出>；只含 ts_start 在 [start, end) 的 bar。"""
        ind = create(name, **params)
        out_keys = [o.key for o in ind.outputs]
        days = self._days_with_warmup(symbol, timeframe, ind, start, end, session)
        rows: list[list[Any]] = []
        for day in days:
            if ind.scope == "session":
                ind.on_session_open(day)
            df = self.history.day_bars(symbol, timeframe, day, session)
            for r in df.iter_rows(named=True):
                if r["ts_start"] >= end:
                    break
                bar = Bar(
                    symbol,
                    timeframe,
                    r["ts_start"],
                    r["open"],
                    r["high"],
                    r["low"],
                    r["close"],
                    r["volume"],
                    r["vwap"],
                    r["trades"],
                    r["session"],
                    r["ts_end"],
                )
                values = ind.update(bar)
                if r["ts_start"] >= start:
                    rows.append([r["ts_start"], r["ts_end"], *(values.get(k) for k in out_keys)])
        schema = {"ts_start": pl.Int64, "ts_end": pl.Int64, **dict.fromkeys(out_keys, pl.Float64)}
        return pl.DataFrame(rows, schema=schema, orient="row")

    def _days_with_warmup(
        self,
        symbol: str,
        timeframe: str,
        ind: Indicator,
        start: int,
        end: int,
        session: SessionFilter,
    ) -> list[TradingDay]:
        cal = self.history.cal
        days = cal.days_overlapping(start, end)
        if not days:
            return []
        spec = ind.warmup()
        first = days[0]
        earliest = first.day
        if spec.prev_sessions:
            d = first.day
            for _ in range(spec.prev_sessions):
                d = cal.previous_trading_day(d)
            earliest = min(earliest, d)
        if spec.bars:
            # 往前逐日累计 start 之前的 bar 数，直到够数
            have = (
                self.history.day_bars(symbol, timeframe, first, session)
                .filter(pl.col("ts_start") < start)
                .height
            )
            d = first.day
            looked = 0
            while have < spec.bars and looked < MAX_LOOKBACK_DAYS:
                d = cal.previous_trading_day(d)
                looked += 1
                have += self.history.day_bars(symbol, timeframe, cal.trading_day(d), session).height
            earliest = min(earliest, d)
        before = cal.trading_days(earliest, first.day)[:-1]
        return before + days
