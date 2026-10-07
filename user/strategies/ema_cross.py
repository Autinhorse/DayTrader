"""示例策略 1：EMA 均线交叉（DESIGN.md 7.5）。

快线上穿慢线做多，下穿平多；allow_short=True 时下穿做空、上穿平空。
按金额下单：每笔约 notional 美元。收盘前平仓由框架统一处理，策略不用自己写。
"""

from pydantic import Field

from trader.core.models import Bar, Reason
from trader.strategy.base import Strategy, StrategyParams, register_strategy


class EmaCrossParams(StrategyParams):
    symbol: str = "SPY"
    timeframe: str = "1m"
    fast: int = Field(default=9, ge=1, le=500)
    slow: int = Field(default=21, ge=2, le=1000)
    notional: float = Field(default=10_000, gt=0, description="每笔金额（美元）")
    allow_short: bool = False


@register_strategy
class EmaCross(Strategy):
    name = "ema_cross"
    description = "EMA 快慢线交叉入场，反向交叉出场"
    Params = EmaCrossParams

    def on_start(self) -> None:
        p = self.params
        assert isinstance(p, EmaCrossParams)
        self.p = p
        self.fast = self.ctx.indicator("ema", p.symbol, p.timeframe, period=p.fast)
        self.slow = self.ctx.indicator("ema", p.symbol, p.timeframe, period=p.slow)

    def on_bar(self, bar: Bar) -> None:
        p = self.p
        if bar.symbol != p.symbol or bar.timeframe != p.timeframe:
            return
        f0, s0, f1, s1 = self.fast.value, self.slow.value, self.fast[-1], self.slow[-1]
        if None in (f0, s0, f1, s1):
            return
        assert f0 is not None and s0 is not None and f1 is not None and s1 is not None
        up = f1 <= s1 and f0 > s0
        down = f1 >= s1 and f0 < s0
        if not (up or down):
            return
        snap = {"fast": f0, "slow": s0, "close": bar.close}
        pos = self.ctx.position(p.symbol).qty
        if self.ctx.open_orders(p.symbol):
            return  # 上一张订单还没结束
        qty = max(int(p.notional // bar.close), 1)
        if up:
            if pos < 0:
                self.ctx.close_position(p.symbol, reason=Reason("ema_cross_up_cover", context=snap))
            elif pos == 0:
                self.ctx.buy(p.symbol, qty, reason=Reason("ema_cross_up", context=snap))
        else:
            if pos > 0:
                self.ctx.close_position(p.symbol, reason=Reason("ema_cross_down", context=snap))
            elif pos == 0 and p.allow_short:
                self.ctx.sell(p.symbol, qty, reason=Reason("ema_cross_down_short", context=snap))
