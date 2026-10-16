"""从项目的历史查询服务读取一只证券的数据，组装成每日计算的输入（规范 1、2.2、2.3）。

全部价格按拆股复权到区间末日的股本口径（与 load_bars(adjusted=True) 相同），比例类指标不受影响，
输出的价格列在拆股前后连续；成本分档另外按当天实际（未复权）开盘价。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import polars as pl

from trader.analytics.cleaning import CleanResult, clean_day, resample_close
from trader.analytics.daily_metrics import Bars, DayInput
from trader.analytics.extended_hours import EthDay
from trader.analytics.params import ReportParams
from trader.core.timeutil import timeframe_ns
from trader.core.trading_calendar import TradingDay
from trader.data.corporate_actions import load_actions, split_factor
from trader.data.history import HistoryService


def to_bars(df: pl.DataFrame, factor: float = 1.0) -> Bars:
    """factor：原始价格要除以的拆股比例（成交量相应乘以它）。"""

    def col(name: str) -> np.ndarray:
        return df[name].fill_null(float("nan")).to_numpy().astype(np.float64)

    return Bars(
        ts=df["ts_start"].to_numpy().astype(np.int64),
        open=col("open") / factor,
        high=col("high") / factor,
        low=col("low") / factor,
        close=col("close") / factor,
        volume=col("volume") * factor,
        vwap=col("vwap") / factor,
    )


def adr_series(ranges: np.ndarray, n: int, min_days: int | None = None) -> np.ndarray:
    """第 i 天收盘后的 ADR（含第 i 天）：最近 n 个 R 的中位数，不足 min_days（默认 n）个为 nan。
    R 为 nan 的日子（提前收盘）跳过，不占名额。用中位数，少数大行情日不会把它拉高。"""
    need = n if min_days is None else min_days
    out = np.full(len(ranges), np.nan)
    window: list[float] = []
    for i, r in enumerate(ranges):
        if math.isfinite(r):
            window.append(float(r))
            if len(window) > n:
                window.pop(0)
        if len(window) >= need:
            out[i] = float(np.median(window))
    return out


@dataclass
class SymbolDays:
    symbol: str
    inputs: dict[date, DayInput] = field(default_factory=dict)
    early_close: set[date] = field(default_factory=set)
    no_data: list[date] = field(default_factory=list)  # 区间内本地没有数据的交易日
    adr_missing: list[date] = field(default_factory=list)  # 之前的数据不足 adr_days 天，ADR 为空
    eth: dict[date, EthDay] = field(default_factory=dict)  # 扩展时段（include_extended_hours）
    path_close: dict[date, tuple[np.ndarray, np.ndarray]] = field(default_factory=dict)


@dataclass
class _Day:
    td: TradingDay
    factor: float
    clean: CleanResult
    raw_open: float
    limit: float


def load_symbol(
    history: HistoryService, symbol: str, start: date, end: date, p: ReportParams
) -> SymbolDays:
    """读一只证券区间内（及之前 ADR 需要的）每天的 bar，按决策 0011 清洗后组装每日输入。

    清洗用的离群阈值 = print_filter_adr × 原始振幅的 ADR（之前不足 5 天时用当天原始振幅），
    清洗后的振幅再算报表用的 ADR，所以坏数据既不影响摆动，也不影响 ADR。"""
    cal = history.cal
    out = SymbolDays(symbol)
    have = set(history.coverage(symbol))
    # 提前收盘日不计入 ADR，多往前取几天
    lookback = start - timedelta(days=p.adr_days * 2 + 30)
    all_days = cal.trading_days(lookback, end)
    in_range = [d for d in all_days if d.day >= start]
    before = [d for d in all_days if d.day < start and d.day in have]
    n_before = p.adr_days + sum(1 for d in before[-(p.adr_days + 5) :] if d.early_close) + 1
    days = before[-n_before:] + [d for d in in_range if d.day in have]
    out.no_data = [d.day for d in in_range if d.day not in have]

    actions = load_actions(history.data_dir)
    windows = p.bad_windows or ()
    loaded: list[_Day] = []
    raw_ranges: list[float] = []
    for td in days:
        m1df = history.day_bars(symbol, "1m", td, "rth")
        if m1df.is_empty():
            if td.day >= start:
                out.no_data.append(td.day)
            continue
        f = split_factor(actions, symbol, td.day, end)
        m1 = to_bars(m1df, f)
        raw_rng = float(m1.high.max() - m1.low.min())
        prior = adr_series(np.array(raw_ranges), p.adr_days, min_days=5)
        limit = p.print_filter_adr * (
            prior[-1] if len(prior) and math.isfinite(prior[-1]) else raw_rng
        )
        raw_ranges.append(np.nan if td.early_close else raw_rng)
        fine_df = history.day_bars(symbol, "1s", td, "rth")
        fine = to_bars(fine_df, f) if not fine_df.is_empty() else _empty()
        win = [w.ns_range() for w in windows if w.applies(symbol, td.day)]
        res = clean_day(m1, fine, limit, win) if len(fine) else CleanResult(fine, m1, 0, 0, 0)
        if len(res.m1) == 0:
            if td.day >= start:
                out.no_data.append(td.day)
            continue
        loaded.append(_Day(td, f, res, float(m1df["open"][0]), limit))
    if not loaded:
        return out
    rng = np.array(
        [
            np.nan if d.td.early_close else d.clean.m1.high.max() - d.clean.m1.low.min()
            for d in loaded
        ]
    )
    adr = adr_series(rng, p.adr_days)
    path_ns = timeframe_ns(p.path_bar_size)

    first = next((i for i, d in enumerate(loaded) if d.td.day >= start), len(loaded))
    prev_eth: EthDay | None = None  # 前一交易日的扩展时段
    if p.include_extended_hours and first > 0:
        d0 = loaded[first - 1]
        prev_eth = _eth(history, symbol, d0.td, d0.factor, p)
    for i, d in enumerate(loaded):
        if i < first:
            continue
        td = d.td
        day_adr = float(adr[i - 1]) if i > 0 and math.isfinite(adr[i - 1]) else math.nan
        pc = float(loaded[i - 1].clean.m1.close[-1]) if i > 0 else math.nan
        if not math.isfinite(day_adr):
            out.adr_missing.append(td.day)
        if td.early_close:
            out.early_close.add(td.day)
        m1 = d.clean.m1
        path = resample_close(m1, td.open, path_ns)
        out.path_close[td.day] = (path.ts, path.close)
        fine = d.clean.fine
        out.inputs[td.day] = DayInput(
            day_open=td.open,
            day_close=td.close,
            m1=m1,
            path_bars=path,
            adr=day_adr,
            pc=pc,
            fine=_fine_slicer(fine) if len(fine) else None,
            raw_open=d.raw_open,
            fixed_prints=d.clean.fixed_prints,
            changed_minutes=d.clean.changed_minutes,
            window_minutes=d.clean.window_minutes,
            filter_limit=d.limit,
        )
        if p.include_extended_hours:
            eth = _eth(history, symbol, td, d.factor, p)
            out.eth[td.day] = eth
            eth.prev_post_last = prev_eth.post_last if prev_eth is not None else math.nan
            prev_eth = eth
    return out


def _empty() -> Bars:
    e = np.empty(0)
    return Bars(np.empty(0, dtype=np.int64), e, e, e, e, e, e)


def _eth(history: HistoryService, symbol: str, td: TradingDay, f: float, p: ReportParams) -> EthDay:
    ext = history.day_bars(symbol, "1m", td, "extended")
    return EthDay.from_frame(ext, f, p.eth_min_bar_volume)


def _fine_slicer(b: Bars):  # noqa: ANN202
    """摆动算法遇到歧义分钟时，取这一分钟的（清洗后的）1 秒 bar。"""

    def fine(t0: int, t1: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None:
        i, j = np.searchsorted(b.ts, [t0, t1])
        return b.ts[i:j], b.open[i:j], b.high[i:j], b.low[i:j]

    return fine
