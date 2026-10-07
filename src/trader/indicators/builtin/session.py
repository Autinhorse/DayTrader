"""会话指标：日内 VWAP、开盘区间高低点、前一日高低收。每个交易日开始时重新开始。"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from trader.core.models import Bar
from trader.core.timeutil import NS_PER_MIN
from trader.core.trading_calendar import TradingDay
from trader.indicators.base import (
    Indicator,
    IndicatorParams,
    OutputSpec,
    WarmupSpec,
    register_indicator,
)

Anchor = Literal["regular", "day"]


class VwapParams(IndicatorParams):
    anchor: Anchor = Field(
        "regular",
        description="regular：从常规时段开盘起算，盘前没有值；day：从交易日第一个时段起算",
    )


@register_indicator
class VWAP(Indicator):
    name = "vwap"
    description = "日内成交量加权均价（按交易日锚定）"
    Params = VwapParams
    outputs = [OutputSpec("value", "line", "price")]
    scope = "session"

    def __init__(self, params: VwapParams) -> None:
        super().__init__(params)
        self.p = params
        self._pv = 0.0
        self._v = 0.0
        self._open: int | None = None

    def warmup(self) -> WarmupSpec:
        return WarmupSpec(from_session_start=True)

    def on_session_open(self, day: TradingDay) -> None:
        self._pv = self._v = 0.0
        self._open = day.open

    def update(self, bar: Bar) -> dict[str, float | None]:
        if self.p.anchor == "regular" and (self._open is None or bar.ts_start < self._open):
            return {"value": None}
        # 有 vwap 用 bar 自己的 vwap（更准确），否则用典型价
        px = bar.vwap if bar.vwap is not None else (bar.high + bar.low + bar.close) / 3
        self._pv += px * bar.volume
        self._v += bar.volume
        return {"value": self._pv / self._v if self._v > 0 else None}


class OpeningRangeParams(IndicatorParams):
    minutes: int = Field(30, ge=1, le=390, description="开盘后多少分钟")


@register_indicator
class OpeningRange(Indicator):
    name = "opening_range"
    description = "常规时段开盘后 N 分钟的最高最低价；区间形成之前没有值。周期应能整除 N"
    Params = OpeningRangeParams
    outputs = [OutputSpec("high", "hline", "price"), OutputSpec("low", "hline", "price")]
    scope = "session"

    def __init__(self, params: OpeningRangeParams) -> None:
        super().__init__(params)
        self.p = params
        self._start: int | None = None
        self._end: int | None = None
        self._hi: float | None = None
        self._lo: float | None = None

    def warmup(self) -> WarmupSpec:
        return WarmupSpec(from_session_start=True)

    def on_session_open(self, day: TradingDay) -> None:
        self._start = day.open
        self._end = min(day.open + self.p.minutes * NS_PER_MIN, day.close)
        self._hi = self._lo = None

    def update(self, bar: Bar) -> dict[str, float | None]:
        if self._start is None or self._end is None:
            return {"high": None, "low": None}
        if self._start <= bar.ts_start < self._end:
            self._hi = bar.high if self._hi is None else max(self._hi, bar.high)
            self._lo = bar.low if self._lo is None else min(self._lo, bar.low)
        if bar.ts_end < self._end:  # 区间还没形成完
            return {"high": None, "low": None}
        return {"high": self._hi, "low": self._lo}


class PrevDayParams(IndicatorParams):
    sessions: Literal["rth", "extended"] = Field(
        "rth", description="rth：只看常规时段；extended：含盘前盘后"
    )


@register_indicator
class PrevDay(Indicator):
    name = "prev_day"
    description = "前一个交易日的最高、最低、收盘价"
    Params = PrevDayParams
    outputs = [
        OutputSpec("high", "hline", "price"),
        OutputSpec("low", "hline", "price"),
        OutputSpec("close", "hline", "price"),
    ]
    scope = "session"

    def __init__(self, params: PrevDayParams) -> None:
        super().__init__(params)
        self.p = params
        self._cur: list[float] | None = None  # 当天 [高, 低, 收]
        self._prev: list[float] | None = None

    def warmup(self) -> WarmupSpec:
        return WarmupSpec(prev_sessions=1)

    def on_session_open(self, day: TradingDay) -> None:
        if self._cur is not None:
            self._prev = self._cur
        self._cur = None

    def update(self, bar: Bar) -> dict[str, float | None]:
        if self.p.sessions == "extended" or bar.session in (None, "regular"):
            c = self._cur
            if c is None:
                self._cur = [bar.high, bar.low, bar.close]
            else:
                c[0], c[1], c[2] = max(c[0], bar.high), min(c[1], bar.low), bar.close
        p = self._prev
        if p is None:
            return {"high": None, "low": None, "close": None}
        return {"high": p[0], "low": p[1], "close": p[2]}
