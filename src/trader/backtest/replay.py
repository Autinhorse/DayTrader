"""回放会话（DESIGN.md 5.4 定速模式、11.2 回放控制条、11.4 回放不泄露未来数据）。

回放用的是回测引擎本身（同一个主循环、同样的撮合与风控），只是按播放速度一点点推进时钟，
所以全速回放一天得到的订单与回测完全相同（tests/test_replay.py）。

- 播放：advance_by(实际经过的时间 × 倍速)；单步：推进到某个周期的下一根 bar 收盘；
- 跳转：向前跳 = 全速处理中间的事件，持仓和挂单照常演变；向后跳 = 开始新的练习分支，
  从目标时刻空仓开始，策略（若有）重新启动、ctx.state 为空；
  原分支的记录保留，可以查看，但不带入新分支。
- 回放中可以手动下单，也可以随时挂载或停止策略。
- 查询（bars、indicator、fills、partial）一律截止到回放时钟：
  只返回此刻已经收盘并到达可用时间的数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time
from decimal import Decimal
from typing import Any

import polars as pl

from trader.backtest.report import _pnl_stats, build_trades
from trader.brokers.sim.matcher import SimBroker, SimConfig
from trader.core.models import Bar, Order, OrderIntent, Reason
from trader.core.timeutil import NS_PER_DAY, ny_to_ns
from trader.core.trading_calendar import SessionFilter, TradingDay
from trader.data.history import HistoryService
from trader.data.indicator_service import IndicatorService
from trader.engine.engine import MANUAL, ActiveIndicator, BacktestEngine, EngineConfig
from trader.oms.manager import round_price
from trader.oms.risk import RiskLimits
from trader.strategy.base import Strategy, StrategyParams

REPLAY_ID = "replay_strategy"


@dataclass
class ReplaySettings:
    day: date
    symbols: list[str]
    start_time: time = time(9, 30)  # 纽约时间
    session: SessionFilter = "rth"
    initial_cash: float = 100_000
    sim: SimConfig = field(default_factory=lambda: SimConfig())
    risk: RiskLimits = field(default_factory=lambda: RiskLimits())


@dataclass
class Branch:
    """一次练习分支：从 start 开始的独立引擎。"""

    index: int
    start: int
    engine: BacktestEngine
    strategy: tuple[type[Strategy], StrategyParams] | None = None
    label: str = ""


class ReplaySession:
    def __init__(self, history: HistoryService, settings: ReplaySettings) -> None:
        self.history = history
        self.cal = history.cal
        self.st = settings
        if not self.cal.is_trading_day(settings.day):
            raise ValueError(f"{settings.day} 不是交易日")
        self.day: TradingDay = self.cal.trading_day(settings.day)
        self.indicators = IndicatorService(history)
        self.branches: list[Branch] = []
        self.speed = 1.0
        start = max(ny_to_ns(settings.day, settings.start_time), self.day_start)
        self._new_branch(start, None)

    # ---------- 分支 ----------

    @property
    def day_start(self) -> int:
        return self.day.open if self.st.session == "rth" else self.day.start

    @property
    def day_end(self) -> int:
        return self.day.close if self.st.session == "rth" else self.day.end

    @property
    def branch(self) -> Branch:
        return self.branches[-1]

    @property
    def engine(self) -> BacktestEngine:
        return self.branch.engine

    def _new_branch(
        self, start: int, strategy: tuple[type[Strategy], StrategyParams] | None
    ) -> Branch:
        eng = BacktestEngine(
            self.history,
            SimBroker(self.st.sim),
            self.st.risk,
            EngineConfig(
                strategy_id=REPLAY_ID,
                session=self.st.session,
                trade_extended=self.st.session == "extended",
                initial_cash=Decimal(str(self.st.initial_cash)),
                decision_delay_ms=self.st.sim.decision_delay_ms,
                bar_grace_ms=self.st.sim.bar_grace_ms,
                whitelist=frozenset(self.st.symbols),
            ),
        )
        eng.start(start, self.day.end, watch=list(self.st.symbols))
        b = Branch(len(self.branches) + 1, start, eng, label=f"分支 {len(self.branches) + 1}")
        self.branches.append(b)
        if strategy is not None:
            self.attach_strategy(*strategy)
        return b

    # ---------- 推进 ----------

    @property
    def now(self) -> int:
        return self.engine.clock.now()

    @property
    def finished(self) -> bool:
        return self.now >= self.day_end + self.engine.grace

    def advance_to(self, t: int) -> None:
        """向前推进到 t（全速处理中间的全部事件）。"""
        self.engine.advance(min(t, self.day_end + self.engine.grace))

    def advance_by(self, ns: int) -> None:
        self.advance_to(self.now + ns)

    def step_bar(self, symbol: str, timeframe: str) -> Bar | None:
        """单步：推进到 symbol 的下一根 timeframe bar 收盘并可用，返回这根 bar。"""
        self.engine.subscribe(symbol, timeframe, claim=False)
        got: list[Bar] = []

        def listener(bar: Bar) -> None:
            if bar.symbol == symbol and bar.timeframe == timeframe:
                got.append(bar)

        eng = self.engine
        eng.bar_listeners.append(listener)
        try:
            while not got and not self.finished:
                nt = eng.next_time()
                if nt is None:
                    break
                self.advance_to(nt)
        finally:
            eng.bar_listeners.remove(listener)
        return got[0] if got else None

    def seek(self, t: int) -> None:
        """跳转。向后跳开始新的练习分支（空仓，策略重新启动）。"""
        t = max(self.day_start, min(t, self.day_end))
        if t >= self.now:
            self.advance_to(t)
            return
        strategy = self.branch.strategy
        self._new_branch(t, strategy)

    # ---------- 交易 ----------

    def order(
        self,
        symbol: str,
        side: str,
        qty: int,
        order_type: str = "MKT",
        limit_price: float | None = None,
        stop_price: float | None = None,
        take_profit: float | None = None,
        stop_loss: float | None = None,
        note: str = "",
    ) -> Order:
        regular = self.engine.oms.session.session == "regular"
        intent = OrderIntent(
            source=MANUAL,
            symbol=symbol,
            side=side,  # type: ignore[arg-type]
            qty=qty,
            order_type=order_type,  # type: ignore[arg-type]
            reason=Reason("manual", note or "回放中手动下单"),
            limit_price=round_price(limit_price) if limit_price is not None else None,
            stop_price=round_price(stop_price) if stop_price is not None else None,
            take_profit=round_price(take_profit) if take_profit is not None else None,
            stop_loss=round_price(stop_loss) if stop_loss is not None else None,
            outside_rth=not regular and order_type == "LMT",
        )
        return self.engine.manual_order(intent)

    def close_position(self, symbol: str) -> Order | None:
        eng = self.engine
        for o in eng.oms.open_orders(symbol, MANUAL):
            eng.manual_cancel(o.client_order_id)
        pos = eng.portfolio.position(symbol)
        if pos.qty == 0:
            return None
        side = "SELL" if pos.qty > 0 else "BUY"
        if eng.oms.session.session == "regular":
            return self.order(symbol, side, abs(pos.qty), note="手动平仓")
        last = eng.portfolio.last_price.get(symbol, float(pos.avg_cost))
        return self.order(
            symbol,
            side,
            abs(pos.qty),
            "LMT",
            last * (0.99 if side == "SELL" else 1.01),
            note="手动平仓",
        )

    def cancel(self, client_order_id: str) -> None:
        self.engine.manual_cancel(client_order_id)

    def attach_strategy(self, cls: type[Strategy], params: StrategyParams) -> None:
        self.engine.attach_strategy(cls, params)
        self.branch.strategy = (cls, params)

    def detach_strategy(self) -> None:
        self.engine.detach_strategy()
        self.branch.strategy = None

    # ---------- 截止到回放时钟的查询（DESIGN.md 11.4） ----------

    def _available(self, ts_end: int) -> bool:
        return ts_end + self.engine.grace <= self.now

    def bars(self, symbol: str, timeframe: str, lookback_days: int = 3) -> pl.DataFrame:
        """此刻已经收盘并可用的 bar（往前 lookback_days 个交易日）。"""
        start = self.day_start - lookback_days * NS_PER_DAY
        df = self.history.bars(symbol, timeframe, start, self.now, self.st.session)
        return df.filter(pl.col("ts_end") + self.engine.grace <= self.now)

    def indicator_values(
        self, symbol: str, timeframe: str, name: str, params: dict[str, Any], lookback_days: int = 3
    ) -> pl.DataFrame:
        """指标在已可用 bar 上的值。每个值只依赖它之前的 bar，截掉未收盘的部分即可。"""
        start = self.day_start - lookback_days * NS_PER_DAY
        df = self.indicators.compute(
            symbol, timeframe, name, params, start, self.now, self.st.session
        )
        return df.filter(pl.col("ts_end") + self.engine.grace <= self.now)

    def watch_indicator(
        self, symbol: str, timeframe: str, name: str, params: dict[str, Any]
    ) -> ActiveIndicator:
        """界面用的活动指标实例：之后每根收盘 bar 自动更新（不占用标的）。"""
        return self.engine.indicator(name, symbol, timeframe, claim=False, **params)

    def partial(self, symbol: str, timeframe: str) -> Bar | None:
        """正在形成的 bar：只由此刻之前已可用的 1 秒 bar 组成。"""
        self.engine.subscribe(symbol, timeframe, claim=False)
        return self.engine.partial(symbol, timeframe)

    def fills(self, symbol: str | None = None) -> pl.DataFrame:
        from trader.backtest.report import fills_frame

        df = fills_frame(self.engine.result())
        if df.is_empty() or symbol is None:
            return df
        return df.filter(pl.col("symbol") == symbol)

    # ---------- 统计 ----------

    def branch_summary(self, branch: Branch | None = None) -> dict[str, Any]:
        b = branch or self.branch
        res = b.engine.result()
        trades = build_trades(res, self.history, self.st.session)
        closed = trades.filter(pl.col("closed")) if trades.height else trades
        stats = _pnl_stats(closed["net_pnl"].to_list() if closed.height else [])
        p = b.engine.portfolio
        return {
            "branch": b.label,
            "trades": stats["trades"],
            "net_pnl_closed": stats["net_pnl"],
            "win_rate": stats["win_rate"],
            "profit_factor": stats["profit_factor"],
            "equity": p.equity(),
            "unrealized": p.unrealized_pnl(),
            "commission": float(p.commission),
            "open_positions": {s: x.qty for s, x in p.positions.items() if x.qty},
            "trades_table": trades,
        }
