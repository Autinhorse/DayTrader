"""指标计算服务（DESIGN.md 6.5）的历史计算部分：每次调用新建指标实例，按预热需求取数，
返回对齐到 bar 时间戳的表格，供图表和 notebook 使用。不读也不写引擎里的活动实例。

预热起点取三项要求中最早的：固定根数、当前交易日开始、之前 N 个完整交易日。
因此从盘中任意时刻开始计算，日内 VWAP 仍从当天开盘算起，前一日高低收仍取自上一个完整交易日，
与从更早时刻开始计算在同一时刻的数值相同。

细粒度输入的指标（input_timeframe，决策 0018）：同时读输入周期的 bar，按时间顺序先 ingest
收盘时刻不晚于显示 bar 收盘时刻的细 bar，再 update 显示 bar。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

import numpy as np
import polars as pl

from trader.core.models import Bar
from trader.core.timeutil import NS_PER_DAY
from trader.core.trading_calendar import SessionFilter, TradingDay
from trader.data.history import HistoryService
from trader.indicators.base import BarBatch, Indicator, create

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
        rows: list[list[Any]] = []
        for bar, display in self._bars(symbol, timeframe, ind, start, end, session):
            if not display:
                ind.ingest_batch(bar)  # type: ignore[arg-type]
                continue
            assert isinstance(bar, Bar)
            values = ind.update(bar)
            if bar.ts_start >= start:
                rows.append([bar.ts_start, bar.ts_end, *(values.get(k) for k in out_keys)])
        schema = {"ts_start": pl.Int64, "ts_end": pl.Int64, **dict.fromkeys(out_keys, pl.Float64)}
        return pl.DataFrame(rows, schema=schema, orient="row")

    def warm(
        self, ind: Indicator, symbol: str, timeframe: str, upto: int, session: SessionFilter
    ) -> list[Mapping[str, float | None]]:
        """用 upto 时刻之前已收盘的 bar 预热一个新实例（引擎的活动实例用），返回每根 bar 的输出。

        与 compute 用同样的取数规则，所以预热后的实例与历史计算在 upto 之后的数值相同。
        """
        out = []
        for item, display in self._bars(symbol, timeframe, ind, upto, upto, session):
            if display:
                assert isinstance(item, Bar)
                if item.ts_end <= upto:
                    out.append(ind.update(item))
                continue
            assert isinstance(item, BarBatch)
            # 正在形成的显示 bar 里已收盘的细 bar 也要喂入
            n = int(np.searchsorted(item.ts_end, upto, side="right"))
            if n:
                ind.ingest_batch(item.slice(0, n))
        return out

    def _bars(
        self,
        symbol: str,
        timeframe: str,
        ind: Indicator,
        start: int,
        end: int,
        session: SessionFilter,
    ) -> Iterator[tuple[Bar | BarBatch, bool]]:
        """按预热需求从更早的交易日起逐根产生 (bar, True)，ts_start < end。

        会话指标在每个交易日开始时重置。细粒度输入的指标在每根显示 bar 之前插入
        (收盘不晚于它的输入周期 bar 的 BarBatch, False)。
        """
        fine_tf = ind.input_timeframe
        for day in self._days_with_warmup(symbol, timeframe, ind, start, end, session):
            if ind.scope == "session":
                ind.on_session_open(day)
            shown = self._day(symbol, timeframe, day, session, end)
            if fine_tf is None:
                yield from ((b, True) for b in shown)
                continue
            fine = self._batch(symbol, fine_tf, day, session, end)
            cuts = np.searchsorted(fine.ts_end, [b.ts_end for b in shown], side="right")
            i = 0
            for b, j in zip(shown, cuts, strict=True):
                if j > i:
                    yield fine.slice(i, int(j)), False
                    i = int(j)
                yield b, True
            if i < len(fine):
                yield fine.slice(i, len(fine)), False

    def _frame(
        self, symbol: str, timeframe: str, day: TradingDay, session: SessionFilter, end: int
    ) -> pl.DataFrame:
        df = self.history.day_bars(symbol, timeframe, day, session)
        return df.filter(df["ts_start"] < end)

    def _day(
        self, symbol: str, timeframe: str, day: TradingDay, session: SessionFilter, end: int
    ) -> list[Bar]:
        df = self._frame(symbol, timeframe, day, session, end)
        return [
            Bar(symbol, timeframe, *r)
            for r in df.select(
                "ts_start", "open", "high", "low", "close", "volume", "vwap", "trades",
                "session", "ts_end",
            ).iter_rows()
        ]  # fmt: skip

    def _batch(
        self, symbol: str, timeframe: str, day: TradingDay, session: SessionFilter, end: int
    ) -> BarBatch:
        df = self._frame(symbol, timeframe, day, session, end)

        def col(name: str, dtype: Any = np.float64) -> np.ndarray:
            return df[name].fill_null(float("nan")).to_numpy().astype(dtype)

        return BarBatch(
            symbol, timeframe, col("ts_start", np.int64), col("ts_end", np.int64), col("open"),
            col("high"), col("low"), col("close"), col("volume"), col("vwap"),
        )  # fmt: skip

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
        # 涉及的交易日：从 start 所在（或之前最近的）交易日到 end 所在的交易日
        days = [
            d
            for d in cal.days_overlapping(start, max(end, start + 1))
            if d.start < max(end, start + 1)
        ]
        if not days or days[0].start > start:
            prev = [
                d
                for d in cal.days_overlapping(start - 7 * NS_PER_DAY, start + 1)
                if d.start <= start
            ]
            days = prev[-1:] + [d for d in days if not prev or d.day > prev[-1].day]
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
