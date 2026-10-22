"""一个交易日的 bar：读取、清洗、分时段，生成细网格（规范 1、2.3，决策 0017）。

价格一律是当天的实际价格（未复权）。时段（分段）：
    pre          4:00–9:30
    open_cross   9:30:00 那一秒（开盘集合竞价）
    rth          常规时段其余部分
    close_cross  收盘前最后一秒和收盘那一秒（收盘集合竞价；提前收盘日按实际收盘时间）
    post         其余盘后时段
没有 1 秒数据、只能用官方 1 分钟 bar 的日子，竞价无法单独分出，全部算在 rth / post 里。

清洗（决策 0011）：删掉坏数据时段，按前后各 clean_half 根 1 秒 bar 收盘价的中位数修正离群价格。
`prefix(t)` 只用 t 之前已收盘的 bar 重新判断离群（最后 clean_half 根用截断窗口），保证时点正确。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import polars as pl

from trader.analytics.cleaning import (
    _rolling_median,
    drop_windows,
    fix_prints,
    prefix_median,
    select,
)
from trader.analytics.daily_metrics import Bars
from trader.analytics.volume_profile.params import ProfileParams
from trader.core import volume_profile as vp
from trader.core.timeutil import NS_PER_SEC, timeframe_ns
from trader.core.trading_calendar import TradingDay

SEGMENTS = ("pre", "open_cross", "rth", "close_cross", "post")
SEG_CODE = {s: i for i, s in enumerate(SEGMENTS)}
OTHER = -1  # 夜盘等不参与的时段


def session_segments(session: str, p: ProfileParams) -> tuple[str, ...]:
    """读取哪些分段：session 加上竞价开关。"""
    out: list[str] = []
    if session in ("pre", "eth"):
        out.append("pre")
    if session == "rth":
        out.append("rth")
        if not p.exclude_open_cross:
            out.append("open_cross")
        if not p.exclude_close_cross:
            out.append("close_cross")
    if session in ("post", "eth"):
        out.append("post")
    return tuple(out)


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


def segment_codes(ts: np.ndarray, td: TradingDay, bar_ns: int) -> np.ndarray:
    seg = np.full(len(ts), OTHER, dtype=np.int8)
    seg[(ts >= td.pre_open) & (ts < td.open)] = SEG_CODE["pre"]
    seg[(ts >= td.open) & (ts < td.close)] = SEG_CODE["rth"]
    seg[(ts >= td.close) & (ts < td.post_close)] = SEG_CODE["post"]
    if bar_ns == NS_PER_SEC:
        seg[ts == td.open] = SEG_CODE["open_cross"]
        seg[(ts >= td.close - NS_PER_SEC) & (ts < td.close + NS_PER_SEC)] = SEG_CODE["close_cross"]
    return seg


def aggregate(b: Bars, seg: np.ndarray, td: TradingDay, bar_ns: int) -> tuple[Bars, np.ndarray]:
    """把 1 秒 bar 按分段合成更长的 bar（各分段内从分段开始对齐，不跨分段）。"""
    if len(b) == 0:
        return b, seg
    origin = np.where(seg == SEG_CODE["pre"], td.pre_open, td.open)
    origin = np.where(seg == SEG_CODE["post"], td.close, origin)
    key = seg.astype(np.int64) * 10**6 + (b.ts - origin) // bar_ns
    starts = np.flatnonzero(np.r_[True, key[1:] != key[:-1]])
    ends = np.r_[starts[1:], len(b)]
    vol = np.add.reduceat(b.volume, starts)
    pv = np.add.reduceat(np.nan_to_num(b.vwap * b.volume), starts)
    out = Bars(
        ts=origin[starts] + (b.ts[starts] - origin[starts]) // bar_ns * bar_ns,
        open=b.open[starts],
        high=np.maximum.reduceat(b.high, starts),
        low=np.minimum.reduceat(b.low, starts),
        close=b.close[ends - 1],
        volume=vol,
        vwap=np.where(vol > 0, pv / np.where(vol > 0, vol, 1), np.nan),
    )
    return out, seg[starts]


@dataclass
class DayBars:
    """清洗前（只删了坏数据时段）的 bar 和整天清洗用的中位数。

    `full()` 是整天清洗的结果；`prefix(t)` 只用 t 之前已收盘的 bar 清洗。两者只在 t 之前最后
    half 根 bar 上可能不同（截断窗口），更早的 bar 结果相同，developing 据此增量累加。"""

    td: TradingDay
    source_bar: str  # "1s" 或 "1m"（没有 1 秒数据时的官方 1 分钟 bar）
    raw: Bars
    seg: np.ndarray
    bar_ns: int
    limit: float  # 离群阈值（价格）；nan 表示不修正
    half: int
    window_bars: int = 0
    _med: np.ndarray = field(default_factory=lambda: np.zeros(0))

    def __post_init__(self) -> None:
        if self.cleans and len(self.raw):
            self._med = _rolling_median(self.raw.close, self.half)

    @property
    def cleans(self) -> bool:
        return self.source_bar == "1s" and math.isfinite(self.limit)

    @staticmethod
    def load(
        td: TradingDay,
        fine: pl.DataFrame,
        minute: pl.DataFrame | None,
        limit: float,
        windows: list[tuple[int, int]],
        half: int,
    ) -> DayBars:
        """fine：当天的 1 秒 bar（可以为空）；为空时用 minute（官方 1 分钟 bar）。"""
        if not fine.is_empty():
            src, df, bar_ns = "1s", fine, NS_PER_SEC
        elif minute is not None and not minute.is_empty():
            src, df, bar_ns = "1m", minute, timeframe_ns("1m")
        else:
            src, df, bar_ns = "1s", fine, NS_PER_SEC
        df = df.filter((pl.col("ts_start") >= td.start) & (pl.col("ts_start") < td.end))
        b = to_bars(df.sort("ts_start")) if not df.is_empty() else _empty()
        b, dropped = drop_windows(b, windows, bar_ns)
        seg = segment_codes(b.ts, td, bar_ns)
        return DayBars(td, src, b, seg, bar_ns, limit, half, dropped)

    def closed(self, t: int) -> int:
        """t 之前已收盘（ts + bar 长度 ≤ t）的 bar 数。"""
        return int(np.searchsorted(self.raw.ts + self.bar_ns, t, side="right"))

    def settled(self, t: int) -> int:
        """t 时清洗结果已与整天清洗相同的 bar 数。"""
        n = self.closed(t)
        return max(0, n - self.half) if self.cleans else n

    def _cleaned(self, i: int, j: int, med: np.ndarray) -> tuple[Bars, np.ndarray, int]:
        """第 i..j−1 根 bar 按中位数 med（与整段对齐）清洗后的结果。"""
        b = select(self.raw, slice(i, j))
        if not self.cleans:
            return b, self.seg[i:j], 0
        out, keep, changed = fix_prints(b, med[i:j], self.limit)
        return select(out, keep), self.seg[i:j][keep], int(changed.sum())

    def full(self, i: int = 0, j: int | None = None) -> tuple[Bars, np.ndarray, int]:
        """整天清洗后的第 i..j−1 根 bar、分段和被修正的 bar 数。"""
        return self._cleaned(i, len(self.raw) if j is None else j, self._med)

    def prefix(self, t: int, i: int = 0) -> tuple[Bars, np.ndarray]:
        """只用 t 之前已收盘的 bar 清洗，返回其中第 i 根以后的部分。"""
        n = self.closed(t)
        med = prefix_median(self.raw.close, self._med, n, self.half) if self.cleans and n else None
        b, seg, _ = self._cleaned(i, n, med if med is not None else self._med)
        return b, seg


def _empty() -> Bars:
    e = np.empty(0)
    return Bars(np.empty(0, dtype=np.int64), e, e, e, e, e, e)


def to_source(
    b: Bars, seg: np.ndarray, td: TradingDay, source: str, source_bar: str
) -> tuple[Bars, np.ndarray]:
    """按 source 参数合成 1m / 5m（只在有 1 秒数据时；官方 1 分钟 bar 只能再合成到 5m）。"""
    if source == "1s" or (source == "1m" and source_bar == "1m"):
        return b, seg
    return aggregate(b, seg, td, timeframe_ns(source))


def grids(
    b: Bars, seg: np.ndarray, p: ProfileParams, segments: tuple[str, ...] = SEGMENTS
) -> tuple[dict[str, vp.FineProfile], dict[str, float], dict[str, float]]:
    """各分段的细网格。返回 (网格, 输入成交量, 因 min_bar_volume 未参与的成交量)。"""
    out: dict[str, vp.FineProfile] = {}
    vin: dict[str, float] = {}
    small: dict[str, float] = {}
    for name in segments:
        m = seg == SEG_CODE[name]
        if not m.any():
            continue
        floor = p.min_bar_volume_eth if name in ("pre", "post") else p.min_bar_volume
        v = b.volume[m]
        use = v >= floor if floor > 0 else np.ones(len(v), dtype=bool)
        vin[name] = float(np.nansum(v))
        small[name] = float(np.nansum(v[~use]))
        out[name] = vp.distribute(
            b.high[m][use], b.low[m][use], b.close[m][use], v[use], b.vwap[m][use], p.distribution
        )
    return out, vin, small
