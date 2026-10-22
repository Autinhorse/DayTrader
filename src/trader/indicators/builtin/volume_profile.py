"""Volume Profile 指标（决策 0017、0018）：用 1 秒 bar 计算，在任意周期的图上显示。

计算与研究模块（trader.analytics.volume_profile）共用 trader.core.volume_profile；口径相同：
只用常规时段，默认剔除收盘竞价（收盘前最后一秒）；格子 = ADR × bin_adr_frac（ADR 为之前 21 个
非提前收盘日的常规时段振幅中位数，不足 5 天时改用前一日收盘价 × 0.05%）；价值区两侧相等时
偏向前一日收盘价一侧。
区别：指标不读文件，所以不做研究模块的数据清洗（坏数据时段、离群成交）；价格为原始价格，
跨拆股的复合 Profile 不换算。
"""

from __future__ import annotations

import math
from collections import deque

import numpy as np
from pydantic import Field

from trader.core import volume_profile as vp
from trader.core.models import Bar
from trader.core.timeutil import NS_PER_SEC
from trader.core.trading_calendar import TradingDay
from trader.indicators.base import (
    BarBatch,
    Indicator,
    IndicatorParams,
    OutputSpec,
    WarmupSpec,
    register_indicator,
)

ADR_DAYS = 21
ADR_MIN_DAYS = 5
BIN_PCT = 0.0005


class _Common(IndicatorParams):
    value_area_pct: float = Field(default=0.7, gt=0, le=1, description="价值区包含的成交量比例")
    bin_adr_frac: float = Field(default=0.02, gt=0, le=1, description="格子大小（ADR 倍数）")
    exclude_close_cross: bool = Field(default=True, description="剔除收盘竞价")


class _DayState:
    """当天常规时段的 1 秒 bar：先存起来，需要时一次性分配到细网格。"""

    def __init__(self) -> None:
        self.grid = vp.FineProfile.empty()
        # 待分配的 bar：逐根喂入的放 rows，批量喂入的放 chunks（n × 4 数组）；列为高、低、收、量，
        # 量为 0 表示只计入振幅、不计入分布。批量喂入前先把 rows 转成一块，保持时间顺序
        self.rows: list[tuple[float, float, float, float]] = []
        self.chunks: list[np.ndarray] = []
        self.high = -math.inf
        self.low = math.inf
        self.close = math.nan

    def seal_rows(self) -> None:
        if self.rows:
            self.chunks.append(np.array(self.rows))
            self.rows = []

    def flush(self) -> vp.FineProfile:
        self.seal_rows()
        if self.chunks:
            hi, lo, cl, vol = np.concatenate(self.chunks).T
            self.high, self.low = max(self.high, hi.max()), min(self.low, lo.min())
            self.close = float(cl[-1])
            self.grid = vp.merge([self.grid, vp.distribute(hi, lo, cl, vol)])
            self.chunks = []
        return self.grid


class _ProfileBase(Indicator):
    """喂入 1 秒 bar，记录每天的细网格、振幅和收盘价。"""

    scope = "session"
    input_timeframe = "1s"
    keep_days = 1

    def __init__(self, params: _Common) -> None:
        super().__init__(params)
        self.c = params
        self._day: TradingDay | None = None
        self._open = self._close = 0
        self._cur = _DayState()
        self._grids: deque[vp.FineProfile] = deque(maxlen=self.keep_days)
        self._ranges: deque[float] = deque(maxlen=ADR_DAYS)
        self._prev_close = math.nan
        self._size = 1  # 当天的格子大小（分）

    def warmup(self) -> WarmupSpec:
        # 提前收盘日不计入 ADR，多取几天（与研究模块相同）
        return WarmupSpec(prev_sessions=max(ADR_DAYS + 5, self.keep_days))

    def on_session_open(self, day: TradingDay) -> None:
        d, cur = self._day, self._cur
        if d is not None and (cur.rows or cur.chunks or math.isfinite(cur.close)):
            self._grids.append(cur.flush())
            if not d.early_close:
                self._ranges.append(cur.high - cur.low)
            self._prev_close = cur.close
        self._day, self._cur = day, _DayState()
        self._open, self._close = day.open, day.close
        adr = float(np.median(self._ranges)) if len(self._ranges) >= ADR_MIN_DAYS else math.nan
        if math.isfinite(adr):
            self._size = vp.bin_cents("adr", adr=adr, adr_frac=self.c.bin_adr_frac)
        else:
            self._size = vp.bin_cents("pct", price=self._prev_close, pct=BIN_PCT)
        self.on_new_day()

    def on_new_day(self) -> None:  # noqa: B027
        """子类：前一日数据已归档，当天开始。"""

    def ingest(self, bar: Bar) -> None:
        if not (self._open <= bar.ts_start < self._close):
            return
        cross = self.c.exclude_close_cross and bar.ts_start >= self._close - NS_PER_SEC
        self._cur.rows.append((bar.high, bar.low, bar.close, 0.0 if cross else bar.volume))

    def ingest_batch(self, batch: BarBatch) -> None:
        m = (batch.ts_start >= self._open) & (batch.ts_start < self._close)
        if not m.any():
            return
        vol = batch.volume[m]
        if self.c.exclude_close_cross:
            vol = np.where(batch.ts_start[m] >= self._close - NS_PER_SEC, 0.0, vol)
        self._cur.seal_rows()
        self._cur.chunks.append(np.column_stack([batch.high[m], batch.low[m], batch.close[m], vol]))

    def _levels(self, fp: vp.FineProfile) -> vp.Levels | None:
        return vp.levels(fp, self._size, value_area_pct=self.c.value_area_pct,
                         ref_price=self._prev_close)  # fmt: skip


class LevelsParams(_Common):
    days: int = Field(default=1, ge=1, le=60, description="合并天数（1=前一日）")


@register_indicator
class VolumeProfileLevels(_ProfileBase):
    name = "vp_levels"
    description = "Volume Profile：前一日（或之前 N 日合并）的 POC、VAH、VAL，用 1 秒 bar 计算"
    Params = LevelsParams
    outputs = [
        OutputSpec("poc", "hline", "price", "#d62728"),
        OutputSpec("vah", "hline", "price", "#1f77b4"),
        OutputSpec("val", "hline", "price", "#1f77b4"),
    ]

    def __init__(self, params: LevelsParams) -> None:
        self.keep_days = params.days
        super().__init__(params)
        self._out: dict[str, float | None] = {"poc": None, "vah": None, "val": None}

    def on_new_day(self) -> None:
        lv = self._levels(vp.merge(self._grids)) if self._grids else None
        self._out = (
            {"poc": lv.poc, "vah": lv.vah, "val": lv.val} if lv else dict.fromkeys(self._out)
        )

    def update(self, bar: Bar) -> dict[str, float | None]:
        return self._out


class DevelopingParams(_Common):
    pass


@register_indicator
class VolumeProfileDeveloping(_ProfileBase):
    name = "vp_developing"
    description = "Volume Profile：当天开盘到此刻的 POC、VAH、VAL（随时间移动），用 1 秒 bar 计算"
    Params = DevelopingParams
    outputs = [
        OutputSpec("dpoc", "line", "price", "#ff7f0e"),
        OutputSpec("dvah", "line", "price", "#2ca02c"),
        OutputSpec("dval", "line", "price", "#2ca02c"),
    ]

    def update(self, bar: Bar) -> dict[str, float | None]:
        d = self._day
        if d is None or bar.ts_end <= d.open or bar.ts_start >= d.close:
            return {"dpoc": None, "dvah": None, "dval": None}
        lv = self._levels(self._cur.flush())
        if lv is None:
            return {"dpoc": None, "dvah": None, "dval": None}
        return {"dpoc": lv.poc, "dvah": lv.vah, "dval": lv.val}
