"""示例自定义指标：唐奇安通道（过去 N 根 bar 的最高价和最低价）。

新增指标只需在 user/indicators/ 下放一个这样的文件：
  1. 定义参数模型（继承 IndicatorParams），每个参数带默认值；
  2. 定义指标类（继承 Indicator），写好 name、Params、outputs，实现 update；
  3. 加上 @register_indicator。
不需要改其他任何代码。只能导入 trader.core 和 trader.indicators，不能读写文件、联网或读系统时间。
"""

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


class DonchianParams(IndicatorParams):
    period: int = Field(20, ge=1, le=1000, description="回看根数")


@register_indicator
class Donchian(Indicator):
    name = "donchian"
    description = "唐奇安通道：过去 period 根 bar 的最高价、最低价和中线"
    Params = DonchianParams
    outputs = [
        OutputSpec("upper", "band", "price"),
        OutputSpec("middle", "line", "price"),
        OutputSpec("lower", "band", "price"),
    ]

    def __init__(self, params: DonchianParams) -> None:
        super().__init__(params)
        self.period = params.period
        self.highs: deque[float] = deque(maxlen=params.period)
        self.lows: deque[float] = deque(maxlen=params.period)

    def warmup(self) -> WarmupSpec:
        return WarmupSpec(bars=self.period)

    def update(self, bar: Bar) -> dict[str, float | None]:
        self.highs.append(bar.high)
        self.lows.append(bar.low)
        if len(self.highs) < self.period:
            return {"upper": None, "middle": None, "lower": None}
        hi, lo = max(self.highs), min(self.lows)
        return {"upper": hi, "middle": (hi + lo) / 2, "lower": lo}
