"""内置指标共用的增量计算部件（不注册为指标）。"""

from __future__ import annotations

import math
from collections import deque
from typing import Literal

from trader.core.models import Bar

Source = Literal["open", "high", "low", "close", "hl2", "hlc3", "ohlc4"]


def price(bar: Bar, source: Source) -> float:
    if source == "close":
        return bar.close
    if source == "open":
        return bar.open
    if source == "high":
        return bar.high
    if source == "low":
        return bar.low
    if source == "hl2":
        return (bar.high + bar.low) / 2
    if source == "hlc3":
        return (bar.high + bar.low + bar.close) / 3
    return (bar.open + bar.high + bar.low + bar.close) / 4


class Sma:
    def __init__(self, n: int) -> None:
        self.n = n
        self.window: deque[float] = deque(maxlen=n)

    def update(self, x: float) -> float | None:
        self.window.append(x)
        return math.fsum(self.window) / self.n if len(self.window) == self.n else None


class Ema:
    """前 n 个值的简单平均作为初值，之后 alpha = 2 / (n + 1)（与 TA-Lib 一致）。"""

    def __init__(self, n: int) -> None:
        self.n = n
        self.alpha = 2 / (n + 1)
        self.seed: list[float] = []
        self.value: float | None = None

    def update(self, x: float) -> float | None:
        if self.value is None:
            self.seed.append(x)
            if len(self.seed) == self.n:
                self.value = math.fsum(self.seed) / self.n
                self.seed = []
            return self.value
        self.value += self.alpha * (x - self.value)
        return self.value


class Wilder:
    """Wilder 平滑：前 n 个值的简单平均作为初值，之后 v = (v × (n−1) + x) / n。"""

    def __init__(self, n: int) -> None:
        self.n = n
        self.seed: list[float] = []
        self.value: float | None = None

    def update(self, x: float) -> float | None:
        if self.value is None:
            self.seed.append(x)
            if len(self.seed) == self.n:
                self.value = math.fsum(self.seed) / self.n
                self.seed = []
            return self.value
        self.value = (self.value * (self.n - 1) + x) / self.n
        return self.value
