"""从项目的历史查询服务读取一只证券的数据，组装成每日计算的输入（规范 1、2.2、2.3）。

日内计算用当天的原始价格（比例类指标不受拆股影响，成本分档也要按实际价格）；跨日的量
（前收盘、ATR）先在拆股复权到区间末日的日线上计算，再换回当天的原始价格口径。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import polars as pl

from trader.analytics.daily_metrics import Bars, DayInput
from trader.analytics.params import ReportParams
from trader.core.trading_calendar import TradingDay
from trader.data.corporate_actions import load_actions, split_factor
from trader.data.history import HistoryService


def to_bars(df: pl.DataFrame) -> Bars:
    def col(name: str) -> np.ndarray:
        return df[name].fill_null(float("nan")).to_numpy().astype(np.float64)

    return Bars(
        ts=df["ts_start"].to_numpy().astype(np.int64),
        open=col("open"),
        high=col("high"),
        low=col("low"),
        close=col("close"),
        volume=col("volume"),
        vwap=col("vwap"),
    )


def atr_wilder(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int) -> np.ndarray:
    """第 i 天收盘后的 ATR（含第 i 天）。TR = max(H, PC) − min(L, PC)，第一天没有前收盘用 H − L；
    前 n 天 TR 的简单平均作初始值，之后 Wilder 平滑。不足 n 天为 nan。"""
    m = len(high)
    tr = high - low
    if m > 1:
        pc = close[:-1]
        tr[1:] = np.maximum(high[1:], pc) - np.minimum(low[1:], pc)
    out = np.full(m, np.nan)
    if m < n:
        return out
    a = float(tr[:n].mean())
    out[n - 1] = a
    for i in range(n, m):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


@dataclass
class SymbolDays:
    symbol: str
    inputs: dict[date, DayInput] = field(default_factory=dict)
    early_close: set[date] = field(default_factory=set)
    no_data: list[date] = field(default_factory=list)  # 区间内本地没有数据的交易日
    atr_missing: list[date] = field(default_factory=list)  # 预热不足，ATR 为空


def load_symbol(
    history: HistoryService, symbol: str, start: date, end: date, p: ReportParams
) -> SymbolDays:
    cal = history.cal
    out = SymbolDays(symbol)
    have = set(history.coverage(symbol))
    lookback = start - timedelta(days=p.warmup_days * 2 + 15)
    all_days = cal.trading_days(lookback, end)
    in_range = [d for d in all_days if d.day >= start]
    before = [d for d in all_days if d.day < start and d.day in have][-(p.warmup_days + 1) :]
    days = before + [d for d in in_range if d.day in have]
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
    hi = np.array([x[1] for x in daily])
    lo = np.array([x[2] for x in daily])
    cl = np.array([x[3] for x in daily])
    atr = atr_wilder(hi, lo, cl, p.atr_days)

    for i, (td, *_rest) in enumerate(daily):
        if td.day < start:
            continue
        f = factors[td.day]
        enough = i >= p.warmup_days and math.isfinite(atr[i - 1])
        day_atr = float(atr[i - 1]) * f if enough else math.nan
        pc = float(cl[i - 1]) * f if i > 0 else math.nan
        if not enough:
            out.atr_missing.append(td.day)
        m1 = history.day_bars(symbol, "1m", td, "rth")
        pb = history.day_bars(symbol, p.path_bar_size, td, "rth")
        if m1.is_empty():
            out.no_data.append(td.day)
            continue
        if td.early_close:
            out.early_close.add(td.day)
        out.inputs[td.day] = DayInput(
            day_open=td.open,
            day_close=td.close,
            m1=to_bars(m1),
            path_bars=to_bars(pb),
            atr=day_atr,
            pc=pc,
            fine=_fine_loader(history, symbol, td),
        )
    return out


def _fine_loader(history: HistoryService, symbol: str, td: TradingDay):  # noqa: ANN202
    """按需读取这一天的 1 秒 bar（只有摆动算法遇到歧义分钟时才读，读一次后缓存）。"""
    cache: dict[str, Bars] = {}

    def fine(t0: int, t1: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None:
        if "b" not in cache:
            df = history.day_bars(symbol, "1s", td, "rth")
            if df.is_empty():
                return None
            cache["b"] = to_bars(df)
        b = cache["b"]
        i, j = np.searchsorted(b.ts, [t0, t1])
        return b.ts[i:j], b.open[i:j], b.high[i:j], b.low[i:j]

    return fine
