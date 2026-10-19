"""测试用策略：只为在模拟账户上演练下单流程，不是交易策略。

常规时段里，没有持仓也没有挂单时，每隔 every_min 分钟在 1 分钟 bar 收盘时出一个买入信号
（qty 股，括号单：止盈、止损各在收盘价上下 bracket_pct%）；从成交起持仓满 hold_min 分钟后市价平仓。
收盘前平仓由框架处理。
"""

from pydantic import Field

from trader.core.models import Bar, Fill, Reason
from trader.core.timeutil import NS_PER_MIN
from trader.strategy.base import Strategy, StrategyParams, register_strategy


class DrillSignalParams(StrategyParams):
    symbol: str = "NVDA"
    timeframe: str = "1m"
    qty: int = Field(default=1, ge=1, le=10)
    every_min: int = Field(default=5, ge=1, description="两次信号之间至少间隔的分钟数")
    hold_min: int = Field(default=5, ge=1, description="持仓满这么久后平仓")
    bracket_pct: float = Field(default=0.5, gt=0, description="止盈止损距离（收盘价的百分比）")


@register_strategy
class DrillSignal(Strategy):
    name = "drill_signal"
    description = "测试用：每隔几分钟买 1 股带括号单，持有几分钟后平仓"
    Params = DrillSignalParams

    def on_start(self) -> None:
        p = self.params
        assert isinstance(p, DrillSignalParams)
        self.p = p
        self.ctx.subscribe(p.symbol, p.timeframe)

    def on_fill(self, fill: Fill) -> None:
        # 成交当时就记下入场时间（和成交一起落盘），不等下一根 bar
        if self.ctx.position(self.p.symbol).qty != 0:
            self.ctx.state.setdefault("entered_ns", self.ctx.now())

    def on_bar(self, bar: Bar) -> None:
        p = self.p
        if bar.symbol != p.symbol or bar.timeframe != p.timeframe or bar.session != "regular":
            return
        now = self.ctx.now()
        snap = {"close": bar.close}
        if self.ctx.position(p.symbol).qty != 0:
            entered = self.ctx.state.setdefault("entered_ns", now)
            if now - entered >= p.hold_min * NS_PER_MIN:
                self.ctx.close_position(p.symbol, reason=Reason("drill_time_exit", context=snap))
                self.ctx.state.pop("entered_ns", None)
            return
        self.ctx.state.pop("entered_ns", None)
        if self.ctx.open_orders(p.symbol):
            return
        last = self.ctx.state.get("signal_ns")
        if last is not None and now - last < p.every_min * NS_PER_MIN:
            return
        d = bar.close * p.bracket_pct / 100
        self.ctx.buy(
            p.symbol,
            p.qty,
            reason=Reason("drill_entry", context=snap),
            take_profit=round(bar.close + d, 2),
            stop_loss=round(bar.close - d, 2),
        )
        self.ctx.state["signal_ns"] = now
