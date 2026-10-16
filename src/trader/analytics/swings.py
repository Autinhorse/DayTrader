"""摆动识别（ZigZag，规范 3.4）。

逐根 bar 用最高价和最低价推进。一根 bar 内最高价和最低价谁先出现不知道：两种顺序都试一遍，
结果相同就没有歧义；不同时读这一分钟的 1 秒 bar 按实际顺序处理。1 秒 bar 本身也分不出先后时
（或没有 1 秒数据）按保守规则：取确认摆动较少的顺序，一样多时按开盘价离最高价还是最低价近来定，
并计入 ambiguous_minutes。

收盘时还没确认的最后一段（最后一个转折点 → 收盘价）不算摆动，单独返回（规范 v3 的 last_leg）。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np

# 给定一分钟的起止时间（UTC 纳秒），返回这一分钟的 1 秒 bar：(ts_start, open, high, low)
FineBars = Callable[[int, int], tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None]

_EPS = 1e-9


@dataclass(frozen=True, slots=True)
class Swing:
    direction: int  # +1 向上，-1 向下
    start_ts: int  # 起点（上一转折点）所在 bar 的开始时间，UTC 纳秒
    end_ts: int  # 终点（极值点）所在 bar 的开始时间
    confirm_ts: int  # 回撤达到阈值、确认这次摆动的 bar 的结束时间
    start_price: float
    end_price: float

    @property
    def amplitude(self) -> float:
        return abs(self.end_price - self.start_price)

    @property
    def amplitude_pct(self) -> float:
        return self.amplitude / self.start_price * 100

    @property
    def minutes(self) -> float:
        return (self.end_ts - self.start_ts) / 60e9


@dataclass(frozen=True, slots=True)
class SwingRun:
    swings: list[Swing]  # 已确认的摆动
    ambiguous: int  # 有歧义且无法用 1 秒数据解决的分钟数
    pivot: float  # 最后一个转折点（还没有转折点时是开盘价）
    pivot_ts: int


@dataclass(slots=True)
class _State:
    theta: float
    direction: int  # 0 = 方向未定
    hi: float  # 方向未定阶段：开盘以来的最高价、最低价
    hi_ts: int
    lo: float
    lo_ts: int
    piv: float = 0.0  # 上一个转折点
    piv_ts: int = 0
    ext: float = 0.0  # 当前段的极值点
    ext_ts: int = 0

    def copy(self) -> _State:
        return _State(
            self.theta, self.direction, self.hi, self.hi_ts, self.lo, self.lo_ts,
            self.piv, self.piv_ts, self.ext, self.ext_ts,
        )  # fmt: skip

    def key(self) -> tuple[float | int, ...]:
        """比较两种处理顺序的结果用。方向确定后，开盘以来的最高、最低价不再起作用。"""
        if self.direction == 0:
            return (0, self.hi, self.hi_ts, self.lo, self.lo_ts)
        return (self.direction, self.piv, self.piv_ts, self.ext, self.ext_ts)

    def point(self, p: float, ts: int, confirm_ts: int) -> Swing | None:
        th = self.theta * (1 - _EPS)
        if self.direction == 0:
            if p > self.hi:
                self.hi, self.hi_ts = p, ts
            if p < self.lo:
                self.lo, self.lo_ts = p, ts
            if p >= self.hi and p - self.lo >= th:
                self.direction, self.piv, self.piv_ts = 1, self.lo, self.lo_ts
                self.ext, self.ext_ts = p, ts
            elif p <= self.lo and self.hi - p >= th:
                self.direction, self.piv, self.piv_ts = -1, self.hi, self.hi_ts
                self.ext, self.ext_ts = p, ts
            return None
        d = self.direction
        if (p - self.ext) * d > 0:  # 新极值
            self.ext, self.ext_ts = p, ts
            return None
        if (self.ext - p) * d >= th:  # 从极值回撤到阈值：确认一次摆动
            sw = Swing(d, self.piv_ts, self.ext_ts, confirm_ts, self.piv, self.ext)
            self.piv, self.piv_ts = self.ext, self.ext_ts
            self.direction = -d
            self.ext, self.ext_ts = p, ts
            return sw
        return None


def _run(st: _State, prices: Sequence[float], ts: int, confirm_ts: int) -> list[Swing]:
    out = []
    for p in prices:
        sw = st.point(p, ts, confirm_ts)
        if sw is not None:
            out.append(sw)
    return out


def _bar(
    st: _State, o: float, h: float, lo: float, ts: int, end_ts: int
) -> tuple[_State, list[Swing], bool]:
    """处理一根 bar。返回 (新状态, 新确认的摆动, 是否有歧义)。有歧义时按保守规则选择。"""
    a = st.copy()
    sa = _run(a, (h, lo), ts, end_ts)
    if h - lo < st.theta * (1 - _EPS) and st.direction != 0:
        return a, sa, False  # 振幅不到阈值：一根 bar 里不可能既创新极值又反转，顺序无关
    b = st.copy()
    sb = _run(b, (lo, h), ts, end_ts)
    if a.key() == b.key() and sa == sb:
        return a, sa, False
    if len(sa) != len(sb):
        return (a, sa, True) if len(sa) < len(sb) else (b, sb, True)
    high_first = abs(o - h) <= abs(o - lo)
    return (a, sa, True) if high_first else (b, sb, True)


def find_swings(
    ts: np.ndarray,
    open_: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    theta: float,
    bar_ns: int = 60 * 10**9,
    fine: FineBars | None = None,
    fine_ns: int = 10**9,
) -> SwingRun:
    """一天的摆动。theta 为价格单位的阈值。从第一根 bar 的开盘价开始；收盘时尚未确认的最后一段
    不计入 swings，它的起点是返回的 pivot。"""
    if len(ts) == 0 or not np.isfinite(theta) or theta <= 0:
        p0 = float(open_[0]) if len(ts) else float("nan")
        return SwingRun([], 0, p0, int(ts[0]) if len(ts) else 0)
    p0 = float(open_[0])
    st = _State(theta, 0, p0, int(ts[0]), p0, int(ts[0]))
    swings: list[Swing] = []
    ambiguous = 0
    for i in range(len(ts)):
        t = int(ts[i])
        nxt, new, amb = _bar(st, float(open_[i]), float(high[i]), float(low[i]), t, t + bar_ns)
        if amb:
            got = fine(t, t + bar_ns) if fine is not None else None
            if got is not None and len(got[0]) > 0:
                fts, fo, fh, fl = got
                st2, new2, amb2 = st, [], False
                for j in range(len(fts)):
                    tj = int(fts[j])
                    st2, s, a = _bar(
                        st2, float(fo[j]), float(fh[j]), float(fl[j]), tj, tj + fine_ns
                    )
                    new2 += s
                    amb2 = amb2 or a
                nxt, new = st2, new2
                ambiguous += int(amb2)
            else:
                ambiguous += 1
        st = nxt
        swings += new
    if st.direction == 0:
        return SwingRun(swings, ambiguous, p0, int(ts[0]))
    return SwingRun(swings, ambiguous, st.piv, st.piv_ts)
