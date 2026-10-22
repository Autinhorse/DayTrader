"""识别摆动之前的数据清洗（决策 0011）：删除坏数据时段，修正离群成交，再用清洗后的 1 秒 bar
重算 1 分钟 bar 的开高低收。只用于本报表；原始数据、图表和回测用的数据不变。

离群成交：1 秒 bar 的某个价格偏离前后各 30 根 1 秒 bar 收盘价的中位数超过 limit（价格单位）。
偏离的价格换成同一根 bar 里正常的价格；开盘价和收盘价都离群时删掉这根 bar。中位数是居中的，
单边快速行情里 bar 自己就在中位数附近，不会被误删；被修正的是"冒出去又马上回来"的价格。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yaml

from trader.analytics.daily_metrics import Bars

NY = ZoneInfo("America/New_York")
MIN_NS = 60 * 10**9


@dataclass(frozen=True, slots=True)
class BadWindow:
    """数据源的坏数据时段（美东时间，[start, end)）。symbols 为空表示所有证券。"""

    day: date
    start: time
    end: time
    symbols: tuple[str, ...] = ()
    note: str = ""

    def applies(self, symbol: str, day: date) -> bool:
        return day == self.day and (not self.symbols or symbol in self.symbols)

    def ns_range(self) -> tuple[int, int]:
        a = datetime.combine(self.day, self.start, NY)
        b = datetime.combine(self.day, self.end, NY)
        return int(a.timestamp() * 1e9), int(b.timestamp() * 1e9)


def load_bad_windows(path: Path) -> tuple[BadWindow, ...]:
    """读 config/bad_data_windows.yaml；文件不存在时为空。"""
    if not path.exists():
        return ()
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    out = []
    for r in raw:
        syms = r.get("symbols", "all")
        out.append(
            BadWindow(
                day=r["date"] if isinstance(r["date"], date) else date.fromisoformat(r["date"]),
                start=time.fromisoformat(str(r["start"])),
                end=time.fromisoformat(str(r["end"])),
                symbols=() if syms in (None, "all") else tuple(s.upper() for s in syms),
                note=str(r.get("note", "")),
            )
        )
    return tuple(out)


@dataclass(frozen=True, slots=True)
class CleanResult:
    fine: Bars  # 清洗后的 1 秒 bar
    m1: Bars  # 用清洗后的 1 秒 bar 重算开高低收的 1 分钟 bar（成交量、vwap 沿用原来的）
    fixed_prints: int  # 被修正或删除的 1 秒 bar 数
    changed_minutes: int  # 开高低收因此改变的 1 分钟 bar 数
    window_minutes: int  # 落在坏数据时段里、被删掉的 1 分钟 bar 数


def _rolling_median(x: np.ndarray, half: int) -> np.ndarray:
    """居中的滚动中位数。开头和结尾不够 half 根时缩小成对称窗口（第 i 根用前后各 i 根），
    否则单边窗口会落后于开盘、收盘时的快速行情，把正常价格当成离群。"""
    n = len(x)
    out = pd.Series(x).rolling(2 * half + 1, center=True, min_periods=1).median().to_numpy().copy()
    for i in range(min(half, n)):
        out[i] = np.median(x[: 2 * i + 1])
        j = n - 1 - i
        out[j] = np.median(x[n - 1 - 2 * i :])
    return out


def prefix_median(close: np.ndarray, med_full: np.ndarray, n: int, half: int = 30) -> np.ndarray:
    """只有前 n 根 bar 时的滚动中位数（等于 _rolling_median(close[:n])），复用整段的结果 med_full：
    前 n − half 根的窗口都在前 n 根以内，与整段相同；只重算最后 half 根（截断窗口）。"""
    if n <= 4 * half + 2:
        return _rolling_median(close[:n], half)
    out = med_full[:n].copy()
    for i in range(half):
        out[n - 1 - i] = np.median(close[n - 1 - 2 * i : n])
    return out


def fix_prints(fine: Bars, med: np.ndarray, limit: float) -> tuple[Bars, np.ndarray, np.ndarray]:
    """按给定的中位数修正离群价格。

    返回 (修正后的 bar（与输入逐根对应）, 保留的掩码, 被改动的掩码)。"""
    n = len(fine)
    if n == 0 or not np.isfinite(limit) or limit <= 0:
        return fine, np.ones(n, dtype=bool), np.zeros(n, dtype=bool)

    def ok(x: np.ndarray) -> np.ndarray:
        return np.abs(x - med) <= limit

    ok_o, ok_h, ok_l, ok_c = ok(fine.open), ok(fine.high), ok(fine.low), ok(fine.close)
    o = np.where(ok_o, fine.open, fine.close)
    c = np.where(ok_c, fine.close, fine.open)
    h = np.where(ok_h, fine.high, np.maximum(o, c))
    lo = np.where(ok_l, fine.low, np.minimum(o, c))
    out = Bars(fine.ts, o, h, lo, c, fine.volume, fine.vwap)
    return out, ok_o | ok_c, ~(ok_o & ok_h & ok_l & ok_c)


def select(b: Bars, mask: np.ndarray | slice) -> Bars:
    return Bars(
        b.ts[mask], b.open[mask], b.high[mask], b.low[mask], b.close[mask],
        b.volume[mask], b.vwap[mask],
    )  # fmt: skip


def clean_fine(fine: Bars, limit: float, half: int = 30) -> tuple[Bars, int]:
    """修正离群的 1 秒 bar。返回 (清洗后的 bar, 被修正或删除的 bar 数)。"""
    n = len(fine)
    if n == 0 or not np.isfinite(limit) or limit <= 0:
        return fine, 0
    out, keep, changed = fix_prints(fine, _rolling_median(fine.close, half), limit)
    return select(out, keep), int(changed.sum())


def drop_windows(b: Bars, windows: list[tuple[int, int]], bar_ns: int) -> tuple[Bars, int]:
    """删掉和坏数据时段有重叠的 bar。返回 (剩下的 bar, 删掉的个数)。"""
    if not windows or len(b) == 0:
        return b, 0
    bad = np.zeros(len(b), dtype=bool)
    for a, z in windows:
        bad |= (b.ts < z) & (b.ts + bar_ns > a)
    if not bad.any():
        return b, 0
    k = ~bad
    out = Bars(b.ts[k], b.open[k], b.high[k], b.low[k], b.close[k], b.volume[k], b.vwap[k])
    return out, int(bad.sum())


def rebuild_minutes(m1: Bars, fine: Bars) -> tuple[Bars, int]:
    """用 1 秒 bar 重算每根 1 分钟 bar 的开高低收；这一分钟没有 1 秒 bar 了就删掉。
    返回 (新的 1 分钟 bar, 开高低收改变的分钟数)。"""
    if len(m1) == 0:
        return m1, 0
    if len(fine) == 0:
        empty = np.empty(0)
        return Bars(np.empty(0, dtype=np.int64), empty, empty, empty, empty, empty, empty), 0
    minute = fine.ts - fine.ts % MIN_NS
    starts = np.flatnonzero(np.r_[True, minute[1:] != minute[:-1]])
    ends = np.r_[starts[1:], len(fine)]
    mts = minute[starts]
    o = fine.open[starts]
    c = fine.close[ends - 1]
    h = np.maximum.reduceat(fine.high, starts)
    lo = np.minimum.reduceat(fine.low, starts)
    idx = np.searchsorted(mts, m1.ts)
    idx_c = np.minimum(idx, len(mts) - 1)
    has = (idx < len(mts)) & (mts[idx_c] == m1.ts)
    j = idx_c[has]
    new = Bars(
        ts=m1.ts[has], open=o[j], high=h[j], low=lo[j], close=c[j],
        volume=m1.volume[has], vwap=m1.vwap[has],
    )  # fmt: skip
    same = (
        np.isclose(new.open, m1.open[has])
        & np.isclose(new.high, m1.high[has])
        & np.isclose(new.low, m1.low[has])
        & np.isclose(new.close, m1.close[has])
    )
    return new, int((~same).sum() + (~has).sum())


def resample_close(m1: Bars, day_open: int, bar_ns: int) -> Bars:
    """把 1 分钟 bar 汇总成更长周期（开盘对齐，整段无成交则缺失）。路程类指标只用收盘价。"""
    if len(m1) == 0:
        return m1
    bucket = (m1.ts - day_open) // bar_ns
    starts = np.flatnonzero(np.r_[True, bucket[1:] != bucket[:-1]])
    ends = np.r_[starts[1:], len(m1)]
    vol = np.add.reduceat(m1.volume, starts)
    return Bars(
        ts=day_open + bucket[starts] * bar_ns,
        open=m1.open[starts],
        high=np.maximum.reduceat(m1.high, starts),
        low=np.minimum.reduceat(m1.low, starts),
        close=m1.close[ends - 1],
        volume=vol,
        vwap=np.full(len(starts), np.nan),
    )


def clean_day(
    m1: Bars, fine: Bars, limit: float, windows: list[tuple[int, int]], half: int = 30
) -> CleanResult:
    fine2, w1 = drop_windows(fine, windows, 10**9)
    m1w, wmin = drop_windows(m1, windows, MIN_NS)
    fine3, fixed = clean_fine(fine2, limit, half)
    new, changed = rebuild_minutes(m1w, fine3)
    del w1
    return CleanResult(fine3, new, fixed, changed, wmin)
