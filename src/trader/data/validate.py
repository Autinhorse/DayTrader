"""单日 1 秒 bar 的校验（DESIGN.md 5.2）。

错误（errors）会让这一天的数据被拒绝写入；提示（warnings）照常写入并记入 catalog 和报告。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl

from trader.core.timeutil import NS_PER_SEC
from trader.core.trading_calendar import TradingDay

DEFAULT_GAP_THRESHOLD_S = 60


@dataclass(slots=True)
class DayCheck:
    rows: int
    errors: dict[str, int] = field(default_factory=dict)
    warnings: dict[str, int] = field(default_factory=dict)
    # 常规时段内超过阈值的无 bar 区间 [start, end)，UTC 纳秒
    gaps: list[tuple[int, int]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _count(df: pl.DataFrame, expr: pl.Expr) -> int:
    return int(df.select(expr.sum()).item() or 0)


def validate_day(
    df: pl.DataFrame, day: TradingDay, gap_threshold_s: int = DEFAULT_GAP_THRESHOLD_S
) -> DayCheck:
    check = DayCheck(rows=df.height)
    if df.is_empty():
        return check
    ts = pl.col("ts_start")

    def put(target: dict[str, int], name: str, n: int) -> None:
        if n:
            target[name] = n

    diffs = ts.diff()
    put(check.errors, "duplicate_ts", _count(df, diffs == 0))
    put(check.errors, "unsorted", _count(df, diffs < 0))
    price_cols = [pl.col(c) for c in ("open", "high", "low", "close")]
    put(
        check.errors,
        "nonpositive_price",
        _count(df, pl.any_horizontal([c <= 0 for c in price_cols])),
    )
    put(check.errors, "negative_volume", _count(df, pl.col("volume") < 0))

    hi, lo = pl.col("high"), pl.col("low")
    bad_ohlc = (hi < pl.max_horizontal("open", "close")) | (lo > pl.min_horizontal("open", "close"))
    put(check.warnings, "ohlc_inconsistent", _count(df, bad_ohlc | (hi < lo)))
    # vwap 计入了不更新 OHLC 的成交类型，可能落在当秒最高最低价之外
    put(
        check.warnings,
        "vwap_outside_range",
        _count(df, (pl.col("vwap") > hi) | (pl.col("vwap") < lo)),
    )
    put(
        check.warnings, "outside_sessions", _count(df, (ts < day.pre_open) | (ts >= day.post_close))
    )

    # 常规时段内的长空档：包括开盘到第一根、最后一根到收盘
    rth = df.filter((ts >= day.open) & (ts < day.close))["ts_start"].to_list()
    threshold = gap_threshold_s * NS_PER_SEC
    edges = [day.open - NS_PER_SEC, *rth, day.close]
    for prev, cur in zip(edges, edges[1:], strict=False):
        start = prev + NS_PER_SEC  # 上一根 bar 结束的时刻
        if cur - start >= threshold:
            check.gaps.append((start, cur))
    put(check.warnings, "rth_gaps", len(check.gaps))
    return check
