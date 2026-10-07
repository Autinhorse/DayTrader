"""示例策略 2：开盘区间突破（DESIGN.md 7.5）。

开盘后前 minutes 分钟的高低点形成区间；之后收盘价突破区间上沿做多（下沿做空，可关闭），
用括号单带止盈止损：止损在区间另一侧，止盈为 reward 倍风险。每天最多入场一次——
"今天是否已经入场"放在 ctx.state 里，重启后不会重复交易。收盘前平仓由框架处理。
"""

from pydantic import Field

from trader.core.models import Bar, Reason
from trader.strategy.base import Strategy, StrategyParams, register_strategy


class OrbParams(StrategyParams):
    symbol: str = "SPY"
    timeframe: str = "5m"
    minutes: int = Field(default=30, ge=5, le=120, description="开盘区间分钟数")
    reward: float = Field(default=2.0, gt=0, description="止盈距离 = reward × 止损距离")
    notional: float = Field(default=10_000, gt=0, description="每笔金额（美元）")
    allow_short: bool = True


@register_strategy
class OrbBreakout(Strategy):
    name = "orb_breakout"
    description = "开盘区间突破，括号单止盈止损，每天最多一次"
    Params = OrbParams

    def on_start(self) -> None:
        p = self.params
        assert isinstance(p, OrbParams)
        self.p = p
        self.orb = self.ctx.indicator("opening_range", p.symbol, p.timeframe, minutes=p.minutes)

    def on_session_open(self) -> None:
        day = self.ctx.today
        if day is not None and self.ctx.state.get("day") != day.day.isoformat():
            self.ctx.state["day"] = day.day.isoformat()
            self.ctx.state["entered"] = False

    def on_bar(self, bar: Bar) -> None:
        p = self.p
        if bar.symbol != p.symbol or bar.timeframe != p.timeframe or bar.session != "regular":
            return
        if self.ctx.state.get("entered"):
            return
        hi, lo = self.orb.get("high"), self.orb.get("low")
        if hi is None or lo is None or hi <= lo:
            return
        qty = max(int(p.notional // bar.close), 1)
        risk = hi - lo
        snap = {"orb_high": hi, "orb_low": lo, "close": bar.close}
        if bar.close > hi:
            self.ctx.buy(
                p.symbol,
                qty,
                reason=Reason("orb_break_up", context=snap),
                take_profit=bar.close + p.reward * risk,
                stop_loss=lo,
            )
            self.ctx.state["entered"] = True
        elif bar.close < lo and p.allow_short:
            self.ctx.sell(
                p.symbol,
                qty,
                reason=Reason("orb_break_down", context=snap),
                take_profit=bar.close - p.reward * risk,
                stop_loss=hi,
            )
            self.ctx.state["entered"] = True
