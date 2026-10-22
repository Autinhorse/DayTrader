"""Volume Profile 的纯计算部分（docs/volume_profile_spec.md 第 2、3 节，决策 0017）。

不读文件、不取时间，只处理 numpy 数组，研究模块、图表指标和策略共用。

- 细网格：每个价格（$0.01 的整数倍，用整数"分"表示）一个成交量。各种时间窗口的 Profile 都是
  细网格相加；分析时再合并成粗格子（`rebin`）。
- 粗格子按价格绝对对齐：第 k 个格子包含 k × b 到 k × b + b − 1 分这 b 个价位。
  格子的"下沿 / 上沿"指其中最低 / 最高的价位，所以全部成交在一个价位时 POC = VAH = VAL。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np

Distribution = Literal["uniform", "close", "typical", "vwap"]
BinMode = Literal["adr", "pct", "fixed"]


@dataclass(frozen=True, slots=True)
class FineProfile:
    """细网格：vol[i] 是价格 (base + i) 分上的成交量。"""

    base: int
    vol: np.ndarray

    @staticmethod
    def empty() -> FineProfile:
        return FineProfile(0, np.zeros(0))

    @property
    def total(self) -> float:
        return float(self.vol.sum())

    def is_empty(self) -> bool:
        return not (self.vol > 0).any()

    def prices(self) -> np.ndarray:
        """每个格子的价格（美元）。"""
        return (self.base + np.arange(len(self.vol))) / 100

    def extent(self) -> tuple[int, int] | None:
        """有成交的最低、最高价位（分）。"""
        nz = np.flatnonzero(self.vol > 0)
        if len(nz) == 0:
            return None
        return self.base + int(nz[0]), self.base + int(nz[-1])

    def __add__(self, other: FineProfile) -> FineProfile:
        return merge([self, other])

    def scaled(self, w: float) -> FineProfile:
        return FineProfile(self.base, self.vol * w)

    def split_adjusted(self, factor: float) -> FineProfile:
        """拆股换算：价格除以 factor，成交量乘以 factor（1 拆 20 为 20）。

        价位 c 分代表 [c − 0.5, c + 0.5) 分，换算后的区间可能覆盖多个新价位（合股时），
        成交量按覆盖的价位平均分配；拆股时多个旧价位落到同一个新价位上。"""
        if factor == 1.0 or self.is_empty():
            return self
        nz = np.flatnonzero(self.vol > 0)
        c = (self.base + nz).astype(np.float64)
        lo = np.floor((c - 0.5) / factor + 0.5).astype(np.int64)
        hi = np.maximum(lo, np.ceil((c + 0.5) / factor - 0.5).astype(np.int64) - 1)
        return _spread(lo, hi, self.vol[nz] * factor)


def _spread(lo: np.ndarray, hi: np.ndarray, vol: np.ndarray) -> FineProfile:
    """把每个 vol[i] 平均分到 lo[i]..hi[i]（分，含两端）。"""
    if len(vol) == 0:
        return FineProfile.empty()
    base = int(lo.min())
    size = int(hi.max()) - base + 2
    per = vol / (hi - lo + 1)
    d = np.bincount(lo - base, per, size) - np.bincount(hi - base + 1, per, size)
    out = np.cumsum(d)[:-1]
    out[out < 0] = 0.0  # 累加的浮点误差
    return FineProfile(base, out)


def _cents(x: np.ndarray) -> np.ndarray:
    return np.rint(np.asarray(x, dtype=np.float64) * 100).astype(np.int64)


def distribute(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    volume: np.ndarray,
    vwap: np.ndarray | None = None,
    method: Distribution = "uniform",
) -> FineProfile:
    """把每根 bar 的成交量分配到细网格（规范 2.1）。价格四舍五入到 $0.01。

    uniform：平均分到 [low, high] 覆盖的每个价位；close / typical / vwap：全部归到一个价位。
    vwap 缺失（nan）的 bar 用 typical。成交量 ≤ 0 或价格无效的 bar 忽略。"""
    high = np.asarray(high, dtype=np.float64)
    low = np.asarray(low, dtype=np.float64)
    close = np.asarray(close, dtype=np.float64)
    volume = np.asarray(volume, dtype=np.float64)
    ok = (volume > 0) & np.isfinite(high) & np.isfinite(low) & np.isfinite(close)
    if not ok.any():
        return FineProfile.empty()
    h, lo_, c, v = high[ok], low[ok], close[ok], volume[ok]
    if method == "uniform":
        a, b = _cents(lo_), _cents(h)
        return _spread(np.minimum(a, b), np.maximum(a, b), v)
    if method == "close":
        p = c
    elif method == "typical":
        p = (h + lo_ + c) / 3
    elif method == "vwap":
        typ = (h + lo_ + c) / 3
        w = np.full(len(v), np.nan) if vwap is None else np.asarray(vwap, np.float64)[ok]
        p = np.where(np.isfinite(w), w, typ)
    else:
        raise ValueError(f"未知的分配方法 {method!r}")
    x = _cents(p)
    return _spread(x, x, v)


def merge(profiles: Iterable[FineProfile], weights: Sequence[float] | None = None) -> FineProfile:
    """细网格相加（可加权）。"""
    items = [p for p in profiles]
    ws = [1.0] * len(items) if weights is None else list(weights)
    pairs = [(p, w) for p, w in zip(items, ws, strict=True) if len(p.vol) and w != 0]
    if not pairs:
        return FineProfile.empty()
    base = min(p.base for p, _ in pairs)
    top = max(p.base + len(p.vol) for p, _ in pairs)
    out = np.zeros(top - base)
    for p, w in pairs:
        out[p.base - base : p.base - base + len(p.vol)] += p.vol * w
    return FineProfile(base, out)


# ---------- 粗格子 ----------


def bin_cents(
    mode: BinMode,
    *,
    adr: float = math.nan,
    price: float = math.nan,
    adr_frac: float = 0.02,
    pct: float = 0.0005,
    fixed: float = 0.05,
) -> int:
    """格子大小（分），向上取整，至少 1 分（规范 2.2）。参考值无效时返回 1。"""
    if mode == "adr":
        x = adr * adr_frac
    elif mode == "pct":
        x = price * pct
    elif mode == "fixed":
        x = fixed
    else:
        raise ValueError(f"未知的格子模式 {mode!r}")
    if not math.isfinite(x) or x <= 0:
        return 1
    return max(1, math.ceil(round(x * 100, 6)))


@dataclass(frozen=True, slots=True)
class Binned:
    """粗格子：vol[i] 是第 first + i 个格子的成交量。

    格子包含 (first + i) × size 分起的 size 个价位。"""

    size: int  # 分
    first: int
    vol: np.ndarray

    def low(self, i: int | np.ndarray) -> np.ndarray:
        return (self.first + np.asarray(i)) * self.size / 100

    def high(self, i: int | np.ndarray) -> np.ndarray:
        return ((self.first + np.asarray(i)) * self.size + self.size - 1) / 100

    def mid(self, i: int | np.ndarray) -> np.ndarray:
        # 整数运算后只做一次除法，避免 226.48000000000002 这样的尾数
        return (2 * (self.first + np.asarray(i)) * self.size + self.size - 1) / 200


def rebin(fp: FineProfile, size: int) -> Binned:
    if len(fp.vol) == 0:
        return Binned(size, 0, np.zeros(0))
    cents = fp.base + np.arange(len(fp.vol))
    k = np.floor_divide(cents, size)
    first = int(k[0])
    return Binned(size, first, np.bincount(k - first, fp.vol))


# ---------- 关键价位 ----------


@dataclass(frozen=True, slots=True)
class Levels:
    bin_size: float  # 美元
    poc: float
    vah: float
    val: float
    profile_high: float
    profile_low: float
    total_volume: float
    hvn: tuple[float, ...]
    hvn_significance: tuple[float, ...]  # 峰值 / 两侧谷值中较高者
    lvn: tuple[float, ...]
    lvn_significance: tuple[float, ...]  # 谷值 / 两侧峰值中较低者（越小越显著）
    poc_position: float  # (POC − 最低价) / (最高价 − 最低价)
    volume_skew: float  # 成交量在价格上的偏度
    va_volume_frac: float  # 价值区实际包含的成交量比例

    @property
    def n_hvn(self) -> int:
        return len(self.hvn)

    def va_width_adr(self, adr: float) -> float:
        return (self.vah - self.val) / adr if adr > 0 else math.nan


def poc_index(b: Binned, center: float) -> int:
    """成交量最大的格子；并列时取中间价最接近成交量加权中心的那个（规范 3.1）。"""
    top = np.flatnonzero(b.vol == b.vol.max())
    if len(top) == 1:
        return int(top[0])
    return int(top[np.argmin(np.abs(b.mid(top) - center))])


def value_area(
    vol: np.ndarray, poc: int, pct: float = 0.7, prefer_up: bool = True
) -> tuple[int, int]:
    """经典价值区算法（规范 3.2）：从 POC 开始，每次比较上方、下方相邻两个格子的成交量之和，
    纳入较大的一侧，直到 ≥ pct × 总量。相等时 prefer_up 为真纳入上方。返回 (最低格子, 最高格子)。"""
    n = len(vol)
    total = float(vol.sum())
    target = pct * total - 1e-9 * total
    lo = hi = poc
    acc = float(vol[poc])
    while acc < target and (lo > 0 or hi < n - 1):
        up = float(vol[hi + 1 : hi + 3].sum()) if hi < n - 1 else -1.0
        dn = float(vol[max(lo - 2, 0) : lo].sum()) if lo > 0 else -1.0
        if up > dn or (up == dn and prefer_up):
            hi = min(hi + 2, n - 1)
            acc += up
        else:
            lo = max(lo - 2, 0)
            acc += dn
    return lo, hi


def smooth(vol: np.ndarray, k: int) -> np.ndarray:
    """k 格居中移动平均；两端只平均实际存在的格子。"""
    if k <= 1 or len(vol) == 0:
        return vol.astype(np.float64)
    w = np.ones(k)
    return np.convolve(vol, w, "same") / np.convolve(np.ones(len(vol)), w, "same")


def nodes(
    s: np.ndarray, hvn_min_frac: float = 0.3, lvn_max_frac: float = 0.3
) -> tuple[list[int], list[int]]:
    """平滑后的分布 s 上的 HVN 与 LVN（规范 3.3）。

    HVN：局部峰值且 ≥ hvn_min_frac × 最大值；
    LVN：相邻两个 HVN 之间的最低点且 ≤ lvn_max_frac × 两峰中较低者。
    相邻两个 HVN 之间没有这样的 LVN 时只保留较高的峰（决策 0017），所以剩下的峰两两之间都有 LVN。"""
    n = len(s)
    if n == 0 or s.max() <= 0:
        return [], []
    floor = hvn_min_frac * s.max()
    peaks = [
        i
        for i in range(n)
        if s[i] > 0
        and s[i] >= floor
        and (i == 0 or s[i] > s[i - 1])
        and (i == n - 1 or s[i] >= s[i + 1])
    ]
    kept: list[int] = []
    for p in peaks:
        while kept:
            q = kept[-1]
            v = float(s[q : p + 1].min())
            if v <= lvn_max_frac * min(s[q], s[p]):
                break
            kept.pop()
            if s[q] >= s[p]:
                p = q
        kept.append(p)
    lvn = [q + int(np.argmin(s[q : p + 1])) for q, p in zip(kept, kept[1:], strict=False)]
    return kept, lvn


def levels(
    fp: FineProfile,
    size: int,
    *,
    value_area_pct: float = 0.7,
    ref_price: float | None = None,
    smooth_bins: int = 3,
    hvn_min_frac: float = 0.3,
    lvn_max_frac: float = 0.3,
) -> Levels | None:
    """从细网格提取关键价位。size：格子大小（分）；ref_price：价值区两侧相等时，纳入靠近它的一侧
    （通常是上一日收盘价）。没有成交时返回 None。"""
    ext = fp.extent()
    if ext is None:
        return None
    lo_px, hi_px = ext[0] / 100, ext[1] / 100
    prices = fp.prices()
    total = fp.total
    center = float((prices * fp.vol).sum() / total)
    b = rebin(fp, size)
    p = poc_index(b, center)
    poc = float(np.clip(b.mid(p), lo_px, hi_px))
    prefer_up = True if ref_price is None or not math.isfinite(ref_price) else ref_price >= poc
    a, z = value_area(b.vol, p, value_area_pct, prefer_up)
    s = smooth(b.vol, smooth_bins)
    hv, lv = nodes(s, hvn_min_frac, lvn_max_frac)

    def valley(i: int, j: int) -> float:
        return float(s[min(i, j) : max(i, j) + 1].min())

    hsig = []
    for k, h in enumerate(hv):
        left = valley(hv[k - 1], h) if k > 0 else valley(0, h)
        right = valley(h, hv[k + 1]) if k + 1 < len(hv) else valley(h, len(s) - 1)
        side = max(left, right)
        hsig.append(float(s[h] / side) if side > 0 else math.inf)
    lsig = [float(s[v] / min(s[hv[k]], s[hv[k + 1]])) for k, v in enumerate(lv)]
    dev = prices - center
    m2 = float((fp.vol * dev**2).sum() / total)
    m3 = float((fp.vol * dev**3).sum() / total)
    return Levels(
        bin_size=size / 100,
        poc=poc,
        vah=float(min(b.high(z), hi_px)),
        val=float(max(b.low(a), lo_px)),
        profile_high=hi_px,
        profile_low=lo_px,
        total_volume=total,
        hvn=tuple(float(np.clip(b.mid(i), lo_px, hi_px)) for i in hv),
        hvn_significance=tuple(hsig),
        lvn=tuple(float(b.mid(i)) for i in lv),
        lvn_significance=tuple(lsig),
        poc_position=(poc - lo_px) / (hi_px - lo_px) if hi_px > lo_px else 0.5,
        volume_skew=m3 / m2**1.5 if m2 > 0 else 0.0,
        va_volume_frac=float(b.vol[a : z + 1].sum() / total),
    )
