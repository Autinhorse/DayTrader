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

from trader.analytics.daily_metrics import Bars, DayInput
from trader.analytics.extended_hours import EthDay
from trader.analytics.params import ReportParams
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


def adr_series(ranges: np.ndarray, n: int) -> np.ndarray:
    """第 i 天收盘后的 ADR（含第 i 天）：最近 n 个 R 的简单平均，不足 n 个为 nan。
    R 为 nan 的日子（提前收盘）跳过，不占名额。"""
    out = np.full(len(ranges), np.nan)
    window: list[float] = []
    for i, r in enumerate(ranges):
        if math.isfinite(r):
            window.append(float(r))
            if len(window) > n:
                window.pop(0)
        if len(window) == n:
            out[i] = sum(window) / n
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


def load_symbol(
    history: HistoryService, symbol: str, start: date, end: date, p: ReportParams
) -> SymbolDays:
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
    factors = {d.day: split_factor(actions, symbol, d.day, end) for d in days}
    daily = []
    for d in days:
        df = history.day_bars(symbol, "1d", d, "rth")
        if df.is_empty():
            continue
        f = factors[d.day]
        daily.append((d, df["high"][0] / f, df["low"][0] / f, df["close"][0] / f))
    if not daily:
        return out
    rng = np.array([np.nan if x[0].early_close else x[1] - x[2] for x in daily])
    cl = np.array([x[3] for x in daily])
    adr = adr_series(rng, p.adr_days)

    first = next((i for i, x in enumerate(daily) if x[0].day >= start), len(daily))
    prev_eth: EthDay | None = None  # 前一交易日的扩展时段
    if p.include_extended_hours and first > 0:
        td0 = daily[first - 1][0]
        prev_eth = _eth(history, symbol, td0, factors[td0.day], p)
    for i, (td, *_rest) in enumerate(daily):
        if i < first:
            continue
        f = factors[td.day]
        day_adr = float(adr[i - 1]) if i > 0 and math.isfinite(adr[i - 1]) else math.nan
        pc = float(cl[i - 1]) if i > 0 else math.nan
        if not math.isfinite(day_adr):
            out.adr_missing.append(td.day)
        m1 = history.day_bars(symbol, "1m", td, "rth")
        pb = history.day_bars(symbol, p.path_bar_size, td, "rth")
        if m1.is_empty():
            out.no_data.append(td.day)
            continue
        if td.early_close:
            out.early_close.add(td.day)
        bars = to_bars(m1, f)
        path = to_bars(pb, f)
        out.path_close[td.day] = (path.ts, path.close)
        out.inputs[td.day] = DayInput(
            day_open=td.open,
            day_close=td.close,
            m1=bars,
            path_bars=path,
            adr=day_adr,
            pc=pc,
            fine=_fine_loader(history, symbol, td, f),
            raw_open=float(m1["open"][0]),
        )
        if p.include_extended_hours:
            eth = _eth(history, symbol, td, f, p)
            out.eth[td.day] = eth
            eth.prev_post_last = prev_eth.post_last if prev_eth is not None else math.nan
            prev_eth = eth
    return out


def _eth(history: HistoryService, symbol: str, td: TradingDay, f: float, p: ReportParams) -> EthDay:
    ext = history.day_bars(symbol, "1m", td, "extended")
    return EthDay.from_frame(ext, f, p.eth_min_bar_volume)


def _fine_loader(history: HistoryService, symbol: str, td: TradingDay, factor: float):  # noqa: ANN202
    """按需读取这一天的 1 秒 bar（只有摆动算法遇到歧义分钟时才读，读一次后缓存）。"""
    cache: dict[str, Bars] = {}

    def fine(t0: int, t1: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None:
        if "b" not in cache:
            df = history.day_bars(symbol, "1s", td, "rth")
            if df.is_empty():
                return None
            cache["b"] = to_bars(df, factor)
        b = cache["b"]
        i, j = np.searchsorted(b.ts, [t0, t1])
        return b.ts[i:j], b.open[i:j], b.high[i:j], b.low[i:j]

    return fine
