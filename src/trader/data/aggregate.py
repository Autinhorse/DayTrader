"""Bar 聚合的向量化实现，供历史查询和图表批量使用。

规则与 trader.core.aggregation.BarAggregator 完全相同（见其文档），由测试保证两者结果一致。
输入一个交易日的 1 秒 bar（store 的列），输出列：
ts_start, ts_end, open, high, low, close, volume, vwap, trades, session。
"""

from __future__ import annotations

import polars as pl

from trader.core.aggregation import check_timeframe, daily_bounds
from trader.core.timeutil import timeframe_ns
from trader.core.trading_calendar import SessionFilter, TradingDay

OUT_SCHEMA: dict[str, pl.DataType] = {
    "ts_start": pl.Int64(),
    "ts_end": pl.Int64(),
    "open": pl.Float64(),
    "high": pl.Float64(),
    "low": pl.Float64(),
    "close": pl.Float64(),
    "volume": pl.Float64(),
    "vwap": pl.Float64(),
    "trades": pl.Int64(),
    "session": pl.String(),
}


def _agg() -> list[pl.Expr]:
    has_vwap = pl.col("vwap").is_not_null()
    pv_volume = pl.col("volume").filter(has_vwap).sum()
    return [
        pl.col("open").first(),
        pl.col("high").max(),
        pl.col("low").min(),
        pl.col("close").last(),
        pl.col("volume").sum(),
        pl.when(pv_volume > 0)
        .then((pl.col("vwap") * pl.col("volume")).filter(has_vwap).sum() / pv_volume)
        .otherwise(None)
        .alias("vwap"),
        # 任何一根输入缺 trades，结果就是 null（与增量实现一致）
        pl.when(pl.col("trades").null_count() == 0)
        .then(pl.col("trades").sum())
        .otherwise(None)
        .alias("trades"),
    ]


def aggregate_day(
    bars_1s: pl.DataFrame, day: TradingDay, timeframe: str, sessions: SessionFilter
) -> pl.DataFrame:
    check_timeframe(timeframe)
    ts = pl.col("ts_start")
    parts: list[pl.DataFrame] = []
    if timeframe == "1d":
        start, end = daily_bounds(day, sessions)
        inside = pl.lit(False)
        for s in day.included(sessions):
            inside = inside | ((ts >= s.start) & (ts < s.end))
        df = bars_1s.filter(inside)
        if df.is_empty():
            return pl.DataFrame(schema=OUT_SCHEMA)
        out = df.select(_agg()).with_columns(
            pl.lit(start).alias("ts_start"),
            pl.lit(end).alias("ts_end"),
            pl.lit(None, dtype=pl.String).alias("session"),
        )
        return out.select(list(OUT_SCHEMA)).cast(OUT_SCHEMA)  # type: ignore[arg-type]

    tf = timeframe_ns(timeframe)
    for s in day.included(sessions):
        df = bars_1s.filter((ts >= s.start) & (ts < s.end))
        if df.is_empty():
            continue
        bucket = (pl.lit(s.start) + (ts - s.start) // tf * tf).alias("bucket")
        g = (
            df.with_columns(bucket)
            .group_by("bucket", maintain_order=True)
            .agg(_agg())
            .with_columns(
                pl.col("bucket").alias("ts_start"),
                pl.min_horizontal(pl.col("bucket") + tf, pl.lit(s.end)).alias("ts_end"),
                pl.lit(s.name).alias("session"),
            )
        )
        parts.append(g.select(list(OUT_SCHEMA)).cast(OUT_SCHEMA))  # type: ignore[arg-type]
    if not parts:
        return pl.DataFrame(schema=OUT_SCHEMA)
    return pl.concat(parts).sort("ts_start")


def label_1s(bars_1s: pl.DataFrame, day: TradingDay, sessions: SessionFilter) -> pl.DataFrame:
    """1 秒 bar 加上 ts_end 和 session 列，只保留纳入的时段，列与 aggregate_day 的输出相同。"""
    ts = pl.col("ts_start")
    label = pl.lit(None, dtype=pl.String)
    for s in reversed(day.included(sessions)):
        label = pl.when((ts >= s.start) & (ts < s.end)).then(pl.lit(s.name)).otherwise(label)
    out = bars_1s.with_columns(
        (ts + timeframe_ns("1s")).alias("ts_end"), label.alias("session")
    ).filter(pl.col("session").is_not_null())
    return out.select(list(OUT_SCHEMA)).cast(OUT_SCHEMA)  # type: ignore[arg-type]
