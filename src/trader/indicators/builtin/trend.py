"""连续指标：SMA、EMA、布林带、MACD、RSI、ATR、成交量均线。"""

from __future__ import annotations

import math
from collections import deque

from pydantic import Field

from trader.core.models import Bar
from trader.indicators.base import (
    Indicator,
    IndicatorParams,
    OutputSpec,
    WarmupSpec,
    register_indicator,
)
from trader.indicators.builtin._common import Ema, Sma, Source, Wilder, price

# EMA/Wilder 类指标用初值近似，预热这么多倍周期后与从头计算的差异可以忽略
CONVERGE = 4


class PeriodParams(IndicatorParams):
    period: int = Field(default=20, ge=1, le=1000, description="周期（根数）")
    source: Source = Field(default="close", description="价格来源")


@register_indicator
class SMA(Indicator):
    name = "sma"
    description = "简单移动平均"
    Params = PeriodParams
    outputs = [OutputSpec("value", "line", "price")]

    def __init__(self, params: PeriodParams) -> None:
        super().__init__(params)
        self.p = params
        self._sma = Sma(params.period)

    def warmup(self) -> WarmupSpec:
        return WarmupSpec(bars=self.p.period)

    def update(self, bar: Bar) -> dict[str, float | None]:
        return {"value": self._sma.update(price(bar, self.p.source))}


@register_indicator
class EMA(Indicator):
    name = "ema"
    description = "指数移动平均（前 period 根的简单平均作初值）"
    Params = PeriodParams
    outputs = [OutputSpec("value", "line", "price")]

    def __init__(self, params: PeriodParams) -> None:
        super().__init__(params)
        self.p = params
        self._ema = Ema(params.period)

    def warmup(self) -> WarmupSpec:
        return WarmupSpec(bars=CONVERGE * self.p.period)

    def update(self, bar: Bar) -> dict[str, float | None]:
        return {"value": self._ema.update(price(bar, self.p.source))}


class BollingerParams(IndicatorParams):
    period: int = Field(default=20, ge=2, le=1000)
    k: float = Field(default=2.0, gt=0, le=10, description="标准差倍数")
    source: Source = "close"


@register_indicator
class Bollinger(Indicator):
    name = "bollinger"
    description = "布林带（总体标准差，与 TA-Lib 一致）"
    Params = BollingerParams
    outputs = [
        OutputSpec("upper", "band", "price"),
        OutputSpec("middle", "line", "price"),
        OutputSpec("lower", "band", "price"),
    ]

    def __init__(self, params: BollingerParams) -> None:
        super().__init__(params)
        self.p = params
        self._w: deque[float] = deque(maxlen=params.period)

    def warmup(self) -> WarmupSpec:
        return WarmupSpec(bars=self.p.period)

    def update(self, bar: Bar) -> dict[str, float | None]:
        self._w.append(price(bar, self.p.source))
        if len(self._w) < self.p.period:
            return {"upper": None, "middle": None, "lower": None}
        mean = math.fsum(self._w) / self.p.period
        sd = math.sqrt(math.fsum((x - mean) ** 2 for x in self._w) / self.p.period)
        return {"upper": mean + self.p.k * sd, "middle": mean, "lower": mean - self.p.k * sd}


class MacdParams(IndicatorParams):
    fast: int = Field(default=12, ge=1, le=500)
    slow: int = Field(default=26, ge=2, le=1000)
    signal: int = Field(default=9, ge=1, le=500)
    source: Source = "close"


@register_indicator
class MACD(Indicator):
    name = "macd"
    description = "MACD：快慢 EMA 之差、信号线与柱"
    Params = MacdParams
    outputs = [
        OutputSpec("macd", "line", "separate"),
        OutputSpec("signal", "line", "separate"),
        OutputSpec("hist", "histogram", "separate"),
    ]

    def __init__(self, params: MacdParams) -> None:
        super().__init__(params)
        if params.fast >= params.slow:
            raise ValueError("fast 必须小于 slow")
        self.p = params
        self._fast, self._slow, self._sig = Ema(params.fast), Ema(params.slow), Ema(params.signal)

    def warmup(self) -> WarmupSpec:
        return WarmupSpec(bars=CONVERGE * self.p.slow + self.p.signal)

    def update(self, bar: Bar) -> dict[str, float | None]:
        x = price(bar, self.p.source)
        f, s = self._fast.update(x), self._slow.update(x)
        if f is None or s is None:
            return {"macd": None, "signal": None, "hist": None}
        m = f - s
        sig = self._sig.update(m)
        return {"macd": m, "signal": sig, "hist": None if sig is None else m - sig}


class RsiParams(IndicatorParams):
    period: int = Field(default=14, ge=1, le=1000)
    source: Source = "close"


@register_indicator
class RSI(Indicator):
    name = "rsi"
    description = "相对强弱指数（Wilder 平滑）"
    Params = RsiParams
    outputs = [OutputSpec("value", "line", "separate")]

    def __init__(self, params: RsiParams) -> None:
        super().__init__(params)
        self.p = params
        self._prev: float | None = None
        self._gain, self._loss = Wilder(params.period), Wilder(params.period)

    def warmup(self) -> WarmupSpec:
        return WarmupSpec(bars=CONVERGE * self.p.period + 1)

    def update(self, bar: Bar) -> dict[str, float | None]:
        x = price(bar, self.p.source)
        prev, self._prev = self._prev, x
        if prev is None:
            return {"value": None}
        g, lo = self._gain.update(max(x - prev, 0.0)), self._loss.update(max(prev - x, 0.0))
        if g is None or lo is None:
            return {"value": None}
        if lo == 0:
            return {"value": 100.0 if g > 0 else 50.0}
        return {"value": 100 - 100 / (1 + g / lo)}


class AtrParams(IndicatorParams):
    period: int = Field(default=14, ge=1, le=1000)


@register_indicator
class ATR(Indicator):
    name = "atr"
    description = "平均真实波幅（Wilder 平滑；第一根没有前收盘，不计）"
    Params = AtrParams
    outputs = [OutputSpec("value", "line", "separate")]

    def __init__(self, params: AtrParams) -> None:
        super().__init__(params)
        self.p = params
        self._prev_close: float | None = None
        self._w = Wilder(params.period)

    def warmup(self) -> WarmupSpec:
        return WarmupSpec(bars=CONVERGE * self.p.period + 1)

    def update(self, bar: Bar) -> dict[str, float | None]:
        pc, self._prev_close = self._prev_close, bar.close
        if pc is None:
            return {"value": None}
        tr = max(bar.high - bar.low, abs(bar.high - pc), abs(bar.low - pc))
        return {"value": self._w.update(tr)}


class VolumeMaParams(IndicatorParams):
    period: int = Field(default=20, ge=1, le=1000)


@register_indicator
class VolumeMA(Indicator):
    name = "volume_ma"
    description = "成交量简单移动平均"
    Params = VolumeMaParams
    outputs = [OutputSpec("value", "line", "separate")]

    def __init__(self, params: VolumeMaParams) -> None:
        super().__init__(params)
        self.p = params
        self._sma = Sma(params.period)

    def warmup(self) -> WarmupSpec:
        return WarmupSpec(bars=self.p.period)

    def update(self, bar: Bar) -> dict[str, float | None]:
        return {"value": self._sma.update(bar.volume)}
