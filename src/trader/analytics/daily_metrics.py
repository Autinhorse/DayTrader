"""每只证券、每个交易日的指标（规范第 3 节）。输入是一天的常规时段 bar（原始价格，numpy 数组），
输出是标量字典、摆动明细和时段指标。缺失的分钟不填充：收益率只在真实存在的 bar 之间计算。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from trader.analytics.params import ReportParams, ksuffix
from trader.analytics.swings import FineBars, Swing, find_swings

MIN_NS = 60 * 10**9


@dataclass(frozen=True, slots=True)
class Bars:
    """一组 bar 的列（按时间排序）。"""

    ts: np.ndarray  # bar 起点，UTC 纳秒
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    vwap: np.ndarray

    def __len__(self) -> int:
        return len(self.ts)


@dataclass(frozen=True, slots=True)
class DayInput:
    day_open: int  # 常规时段开始、结束，UTC 纳秒
    day_close: int
    m1: Bars
    path_bars: Bars  # path_bar_size 周期（默认 5 分钟）
    atr: float  # 前一交易日的 ATR，当天原始价格口径；预热不足为 nan
    pc: float  # 前一日常规时段收盘价，当天原始价格口径；没有为 nan
    fine: FineBars | None = None


@dataclass(frozen=True, slots=True)
class DayResult:
    metrics: dict[str, float]
    swings: dict[float, list[Swing]]  # 阈值 k → 摆动
    buckets: list[dict[str, float]]


def _ratio(a: float, b: float) -> float:
    return a / b if b > 0 and math.isfinite(a) else math.nan


def log_returns(close: np.ndarray) -> np.ndarray:
    return np.diff(np.log(close)) if len(close) > 1 else np.empty(0)


def path_length(open_price: float, close: np.ndarray) -> float:
    """从开盘价出发，依次经过各 bar 收盘价的总路程。"""
    if len(close) == 0:
        return math.nan
    return float(np.abs(np.diff(np.concatenate([[open_price], close]))).sum())


def efficiency(open_price: float, close: np.ndarray) -> float:
    """效率比 |C − O| / 路程：单边直线为 1，往复后回到原点接近 0。"""
    path = path_length(open_price, close)
    return _ratio(abs(float(close[-1]) - open_price), path) if len(close) else math.nan


def autocorr1(r: np.ndarray) -> float:
    if len(r) < 3:
        return math.nan
    a, b = r[:-1], r[1:]
    if a.std() == 0 or b.std() == 0:
        return math.nan
    return float(np.corrcoef(a, b)[0, 1])


def variance_ratio(ts: np.ndarray, close: np.ndarray, day_open: int, bar_ns: int, q: int) -> float:
    """q 倍周期收益率方差 / (q × 原周期收益率方差)。

    长周期收盘价取每个长周期内最后一根存在的 bar。
    """
    r = log_returns(close)
    if len(r) < 2 or r.var() == 0:
        return math.nan
    bucket = (ts - day_open) // (bar_ns * q)
    last = np.r_[bucket[1:] != bucket[:-1], True]
    rq = log_returns(close[last])
    if len(rq) < 2:
        return math.nan
    return float(rq.var() / (q * r.var()))


def cumulative_vwap(b: Bars) -> np.ndarray:
    px = np.where(np.isfinite(b.vwap), b.vwap, b.close)
    vol = np.cumsum(b.volume)
    pv = np.cumsum(px * b.volume)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = pv / vol
    return np.where(vol > 0, out, b.close)


def vwap_crosses(b: Bars, vwap: np.ndarray, hold_min: int, day_close: int) -> int:
    """收盘价穿越当日累计 VWAP、且之后连续 hold_min 分钟（钟表时间）都在新的一侧，计一次。
    缺失的分钟视为留在原来那一侧；剩余时间不足 hold_min 分钟的穿越不计。"""
    side = np.sign(b.close - vwap)
    confirmed = 0
    count = 0
    n = len(side)
    for i in range(n):
        s = side[i]
        if s == 0 or s == confirmed:
            continue
        if confirmed == 0:
            confirmed = s  # 第一次站到某一侧，不算穿越
            continue
        end = b.ts[i] + hold_min * MIN_NS
        if end > day_close:
            break
        j = i
        ok = True
        while j < n and b.ts[j] < end:
            if side[j] == -s:
                ok = False
                break
            j += 1
        if ok:
            count += 1
            confirmed = s
    return count


def gap_minutes(ts: np.ndarray, day_open: int, day_close: int) -> float:
    """最长的连续无成交分钟数（包括开盘到第一根、最后一根到收盘）。"""
    if len(ts) == 0:
        return (day_close - day_open) / MIN_NS
    edges = np.concatenate([[day_open - MIN_NS], ts, [day_close]])
    return float((np.diff(edges) // MIN_NS - 1).max())


def compute_day(inp: DayInput, p: ReportParams) -> DayResult:
    m1 = inp.m1
    o = float(m1.open[0])
    h = float(m1.high.max())
    lo = float(m1.low.min())
    c = float(m1.close[-1])
    rng = h - lo
    atr = inp.atr
    expected = (inp.day_close - inp.day_open) // MIN_NS
    pb = inp.path_bars
    path_ns = int(np.median(np.diff(pb.ts))) if len(pb) > 1 else 5 * MIN_NS
    path_ns = max(MIN_NS, (path_ns // MIN_NS) * MIN_NS)
    r_path = log_returns(pb.close)
    vwap = cumulative_vwap(m1)
    hi_i = int(np.argmax(m1.high))
    lo_i = int(np.argmin(m1.low))
    px = np.where(np.isfinite(m1.vwap), m1.vwap, m1.close)

    met: dict[str, float] = {
        "open": o,
        "high": h,
        "low": lo,
        "close": c,
        "atr": atr,
        "range_pct": rng / o * 100,
        "range_atr": _ratio(rng, atr),
        "gap_atr": _ratio(o - inp.pc, atr) if math.isfinite(inp.pc) else math.nan,
        "rv_5min_pct": float(np.sqrt((r_path**2).sum()) * 100) if len(r_path) else math.nan,
        "parkinson_pct": (math.log(h) - math.log(lo)) / math.sqrt(4 * math.log(2)) * 100,
        "dollar_vol": float((px * m1.volume).sum()),
        "missing_ratio": max(0.0, 1 - len(m1) / expected) if expected > 0 else math.nan,
        "max_gap_min": gap_minutes(m1.ts, inp.day_open, inp.day_close),
        "body_ratio": _ratio(abs(c - o), rng),
        "close_loc": _ratio(c - lo, rng),
        "high_time": (int(m1.ts[hi_i]) - inp.day_open) / MIN_NS,
        "low_time": (int(m1.ts[lo_i]) - inp.day_open) / MIN_NS,
        "path_range_ratio": _ratio(path_length(o, pb.close), rng),
        "efficiency": efficiency(o, pb.close),
        "efficiency_1m": efficiency(o, m1.close),
        "ac1": autocorr1(r_path),
        "vr_3": variance_ratio(pb.ts, pb.close, inp.day_open, path_ns, 3),
        "vwap_crosses": float(vwap_crosses(m1, vwap, p.vwap_cross_min_hold, inp.day_close)),
        "cost_pct": p.cost_pct(o),
    }

    swings: dict[float, list[Swing]] = {}
    ambiguous = 0
    for k in p.swing_thresholds_atr:
        sfx = ksuffix(k)
        theta = k * atr if math.isfinite(atr) else math.nan
        sw, amb = (
            find_swings(m1.ts, m1.open, m1.high, m1.low, theta, MIN_NS, inp.fine)
            if math.isfinite(theta)
            else ([], 0)
        )
        ambiguous += amb
        swings[k] = sw
        met.update(_swing_stats(sw, sfx, theta, atr, met["cost_pct"]))
    met["ambiguous_minutes"] = float(ambiguous)

    buckets = _buckets(inp, p, pb, swings.get(p.score_k, []))
    return DayResult(met, swings, buckets)


def _swing_stats(sw: list[Swing], sfx: str, theta: float, atr: float, cost: float) -> dict:
    if not math.isfinite(theta):
        keys = ("n", "avg_atr", "avg_pct", "max_atr", "avg_minutes", "total_pct", "capturable_pct")
        return {f"swing_{k}_{sfx}": math.nan for k in keys}
    amp = np.array([s.amplitude for s in sw])
    pct = np.array([s.amplitude_pct for s in sw])
    theta_pct = np.array([theta / s.start_price * 100 for s in sw])
    mins = np.array([s.minutes for s in sw])
    n = len(sw)
    return {
        f"swing_n_{sfx}": float(n),
        f"swing_avg_atr_{sfx}": float(amp.mean() / atr) if n else math.nan,
        f"swing_avg_pct_{sfx}": float(pct.mean()) if n else math.nan,
        f"swing_max_atr_{sfx}": float(amp.max() / atr) if n else math.nan,
        f"swing_avg_minutes_{sfx}": float(mins.mean()) if n else math.nan,
        f"swing_total_pct_{sfx}": float(pct.sum()),
        # 转折点要回撤 θ 才能确认：入场、出场各损失约 θ，再扣买卖两次成本
        f"swing_capturable_pct_{sfx}": float(np.maximum(pct - 2 * theta_pct - 2 * cost, 0).sum()),
    }


def _buckets(
    inp: DayInput, p: ReportParams, pb: Bars, swings: list[Swing]
) -> list[dict[str, float]]:
    """按 time_buckets_min 切分：已实现波动率（桶内 path_bar_size 收益率）、方差占全天比例、
    成交量占全天比例、桶内确认的摆动次数（k = score_k）。收益率归入其结束 bar 所在的桶。"""
    width = p.time_buckets_min * MIN_NS
    n = int(-(-(inp.day_close - inp.day_open) // width))
    r = log_returns(pb.close)
    rb = (pb.ts[1:] - inp.day_open) // width if len(r) else np.empty(0, dtype=np.int64)
    var = np.bincount(rb.astype(np.int64), weights=r**2, minlength=n)[:n] if len(r) else np.zeros(n)
    m1 = inp.m1
    vb = ((m1.ts - inp.day_open) // width).astype(np.int64)
    vol = np.bincount(vb, weights=m1.volume, minlength=n)[:n]
    sb = np.array([(s.confirm_ts - 1 - inp.day_open) // width for s in swings], dtype=np.int64)
    cnt = np.bincount(sb, minlength=n)[:n] if len(sb) else np.zeros(n)
    tot_var = var.sum()
    tot_vol = vol.sum()
    out = []
    for i in range(n):
        out.append(
            {
                "bucket": float(i),
                "bucket_minute": float(i * p.time_buckets_min),
                "rv_pct": float(math.sqrt(var[i]) * 100),
                "rv_share": float(var[i] / tot_var) if tot_var > 0 else math.nan,
                "volume_share": float(vol[i] / tot_vol) if tot_vol > 0 else math.nan,
                "swing_n": float(cnt[i]),
            }
        )
    return out
