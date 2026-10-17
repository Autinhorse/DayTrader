"""每只证券、每个交易日的指标（规范第 3 节）。输入是一天的常规时段 bar（复权价格，numpy 数组），
输出是标量字典、摆动明细和时段指标。缺失的分钟不填充：收益率只在真实存在的 bar 之间计算。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from trader.analytics.params import ReportParams, ksuffix, skip_suffix
from trader.analytics.quality import outlier_bars
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
    adr: float  # 前一交易日的 ADR（复权价格）；之前的数据不足 adr_days 天为 nan
    pc: float  # 前一日常规时段收盘价（复权价格）；没有为 nan
    fine: FineBars | None = None
    raw_open: float = math.nan  # 当天实际开盘价（未复权），成本分档用；nan 表示和 m1 相同
    fixed_prints: int = 0  # 清洗：被修正或删除的 1 秒 bar 数（决策 0011）
    changed_minutes: int = 0  # 清洗：开高低收因此改变的 1 分钟 bar 数
    window_minutes: int = 0  # 清洗：落在坏数据时段里被删掉的 1 分钟 bar 数
    filter_limit: float = math.nan  # 清洗用的离群阈值（价格单位）


@dataclass(frozen=True, slots=True)
class LastLeg:
    """收盘时还没确认的最后一段：从最后一个转折点到收盘价。"""

    start_ts: int
    end_ts: int  # 最后一根 bar 的开始时间
    start_price: float
    end_price: float


@dataclass(frozen=True, slots=True)
class DayResult:
    metrics: dict[str, float]
    swings: dict[float, list[Swing]]  # 阈值 k → 摆动
    last_legs: dict[float, LastLeg]  # 阈值 k → 未确认的最后一段
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
    adr = inp.adr
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
        "adr": adr,
        "raw_open": inp.raw_open if math.isfinite(inp.raw_open) else o,
        "range_pct": rng / o * 100,
        "range_adr": _ratio(rng, adr),
        "adr_pct": adr / o * 100,
        "gap_adr": _ratio(o - inp.pc, adr) if math.isfinite(inp.pc) else math.nan,
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
        "outlier_bars": float(
            outlier_bars(
                m1, p.anomaly_outlier_adr * adr, inp.day_open + p.anomaly_skip_open_min * MIN_NS
            )
        )
        if math.isfinite(adr)
        else math.nan,
    }
    met.update(opening_range(inp, o, adr, p.opening_windows_min))
    met["cost_pct"] = p.cost_pct(met["raw_open"])
    met["fixed_prints"] = float(inp.fixed_prints)
    met["changed_minutes"] = float(inp.changed_minutes)
    met["window_minutes"] = float(inp.window_minutes)
    met["filter_limit"] = inp.filter_limit

    swings: dict[float, list[Swing]] = {}
    legs: dict[float, LastLeg] = {}
    ambiguous = 0
    for k in p.swing_thresholds_adr:
        sfx = ksuffix(k)
        theta = k * adr if math.isfinite(adr) else math.nan
        run = find_swings(m1.ts, m1.open, m1.high, m1.low, theta, MIN_NS, inp.fine)
        ambiguous += run.ambiguous
        swings[k] = run.swings
        cutoff = inp.day_open + p.swing_skip_open_min * MIN_NS
        main = [s for s in run.swings if s.start_ts >= cutoff]
        met.update(_swing_stats(main, sfx, theta, adr, met["cost_pct"]))
        if k == p.score_k:
            # 开盘段（开盘到 swing_skip_open_min）的摆动，只作参考
            head = [s for s in run.swings if s.start_ts < cutoff]
            st = _swing_stats(head, "x", theta, adr, met["cost_pct"])
            met["open_swing_n"] = st["swing_n_x"]
            met["swing_n_all"] = float(len(run.swings))  # 全天（含开盘段），特殊日检测用
            met["open_capturable_pct"] = st["swing_capturable_pct_x"]
            # 开盘剔除对照：每个剔除时长各一组（只算打分用到的三项）
            for m in p.open_skip_compare:
                t0 = inp.day_open + m * MIN_NS
                st = _swing_stats([s for s in run.swings if s.start_ts >= t0], "x", theta, adr,
                                  met["cost_pct"])  # fmt: skip
                ss = skip_suffix(k, m)
                for f in ("n", "avg_pct", "capturable_pct"):
                    met[f"swing_{f}_{ss}"] = st[f"swing_{f}_x"]
        if math.isfinite(theta):
            legs[k] = LastLeg(run.pivot_ts, int(m1.ts[-1]), run.pivot, c)
            met[f"last_leg_pct_{sfx}"] = (c - run.pivot) / run.pivot * 100
            met[f"last_leg_adr_{sfx}"] = (c - run.pivot) / adr
        else:
            met[f"last_leg_pct_{sfx}"] = met[f"last_leg_adr_{sfx}"] = math.nan
    met["ambiguous_minutes"] = float(ambiguous)
    score_sw = swings.get(p.score_k, [])
    met["swing_density"] = (
        _ratio(sum(s.amplitude for s in score_sw), rng) if math.isfinite(adr) else math.nan
    )

    buckets = _buckets(inp, p, pb, score_sw)
    return DayResult(met, swings, legs, buckets)


def opening_range(inp: DayInput, o: float, adr: float, windows: tuple[int, ...]) -> dict:
    """开盘区间（开盘后 n 分钟）和之后的走势，全部以 ADR 为单位、相对开盘价（决策 0015）：
    or_hi_n / or_lo_n 区间最高、最低（or_lo 为正数 = 开盘价以下多少）；or_px_n 第 n 分钟末的价格；
    post_hi_n / post_lo_n 第 n 分钟以后的最高、最低（同样的符号约定）；
    brk_up_n / brk_dn_n 第一次突破区间上沿、下沿的时刻（距开盘分钟数，bar 起点），没突破为 nan。"""
    m1 = inp.m1
    out: dict[str, float] = {}
    for n in windows:
        keys = ("or_hi", "or_lo", "or_px", "post_hi", "post_lo", "brk_up", "brk_dn")
        res = dict.fromkeys(keys, math.nan)
        cut = inp.day_open + n * MIN_NS
        head = m1.ts < cut
        if math.isfinite(adr) and adr > 0 and head.any() and (~head).any():
            hi, lo = float(m1.high[head].max()), float(m1.low[head].min())
            tail_h, tail_l, tail_t = m1.high[~head], m1.low[~head], m1.ts[~head]
            up, dn = np.flatnonzero(tail_h > hi), np.flatnonzero(tail_l < lo)
            res = {
                "or_hi": (hi - o) / adr,
                "or_lo": (o - lo) / adr,
                "or_px": (float(m1.close[head][-1]) - o) / adr,
                "post_hi": (float(tail_h.max()) - o) / adr,
                "post_lo": (o - float(tail_l.min())) / adr,
                "brk_up": (int(tail_t[up[0]]) - inp.day_open) / MIN_NS if len(up) else math.nan,
                "brk_dn": (int(tail_t[dn[0]]) - inp.day_open) / MIN_NS if len(dn) else math.nan,
            }
        for k in keys:
            out[f"{k}_{n}"] = res[k]
    return out


def _swing_stats(sw: list[Swing], sfx: str, theta: float, adr: float, cost: float) -> dict:
    if not math.isfinite(theta):
        keys = ("n", "avg_adr", "avg_pct", "max_adr", "avg_minutes", "total_pct", "capturable_pct")
        return {f"swing_{k}_{sfx}": math.nan for k in keys}
    amp = np.array([s.amplitude for s in sw])
    pct = np.array([s.amplitude_pct for s in sw])
    theta_pct = np.array([theta / s.start_price * 100 for s in sw])
    mins = np.array([s.minutes for s in sw])
    n = len(sw)
    return {
        f"swing_n_{sfx}": float(n),
        f"swing_avg_adr_{sfx}": float(amp.mean() / adr) if n else math.nan,
        f"swing_avg_pct_{sfx}": float(pct.mean()) if n else math.nan,
        f"swing_max_adr_{sfx}": float(amp.max() / adr) if n else math.nan,
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
