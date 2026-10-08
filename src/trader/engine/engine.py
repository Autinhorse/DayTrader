"""回测引擎（DESIGN.md 2.3、10.2）：同步、确定性的主循环。

同一时刻 T 的处理顺序固定：
  1. 撮合：可用时间为 T 的 1 秒 bar（刚结束的市场区间）撮合此前已生效的挂单；
  2. 交付：成交和订单更新回调策略 on_fill / on_order；
  3. 行情：更新最新价、聚合器；到期的周期 bar 封口，更新指标，回调 on_bar；
  4. 交易时段事件，然后是定时器。
策略在 2～4 中发出的订单生效时间为 T + 决策延迟，只能参与之后开始的市场区间。
1 秒 bar 的可用时间 = 收盘 + 封口宽限（默认 300 毫秒，与实时一致）。行情稀疏时，
聚合 bar 的封口和定时器照常按时间触发。

1 分钟及以上周期 bar 的成交量、vwap、成交笔数用 Massive 官方 1 分钟 bar 汇总（与历史查询一致，
见 docs/decisions/0003 第 5 条）；开高低收由 1 秒 bar 增量聚合。
"""

from __future__ import annotations

import bisect
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from trader.brokers.sim.matcher import SimBroker
from trader.core.aggregation import BarAggregator, check_timeframe
from trader.core.clock import SimClock
from trader.core.events import BarEvent, Phase, SessionEvent, SessionKind, TimerEvent
from trader.core.models import Bar, Fill, Order, OrderIntent, Position, Reason
from trader.core.scheduler import EventScheduler
from trader.core.timeutil import NS_PER_DAY, NS_PER_MIN, NS_PER_MS, NS_PER_SEC, timeframe_ns
from trader.core.trading_calendar import SessionFilter, TradingDay
from trader.data.history import HistoryService
from trader.data.indicator_service import IndicatorService
from trader.indicators.base import create
from trader.oms.manager import Notice, OrderManager, round_price
from trader.oms.portfolio import Portfolio
from trader.oms.risk import RiskLimits
from trader.strategy.base import Price, Strategy, StrategyParams, StrategyState

HISTORY_KEEP = 2000  # 每个 (标的, 周期) 保留的已收盘 bar 数
VALUES_KEEP = 500  # 每个指标保留的历史值个数

_FLATTEN = "__flatten__"
MANUAL = "manual"  # 手动订单的来源和标的所有者


# ---------- 指标句柄 ----------


class ActiveIndicator:
    """引擎维护的活动指标实例；策略通过它读取数值。"""

    def __init__(self, name: str, params: dict[str, Any], symbol: str, timeframe: str) -> None:
        self.ind = create(name, **params)
        self.symbol, self.timeframe = symbol, timeframe
        self.keys = [o.key for o in self.ind.outputs]
        self.values: deque[Mapping[str, float | None]] = deque(maxlen=VALUES_KEEP)
        self.times: deque[int] = deque(maxlen=VALUES_KEEP)  # 对应 bar 的 ts_start（预热部分为 0）

    def push(self, out: Mapping[str, float | None], ts: int = 0) -> None:
        self.values.append(out)
        self.times.append(ts)

    @property
    def value(self) -> float | None:
        return self.get(self.keys[0])

    def __getitem__(self, i: int) -> float | None:
        """handle[-1] 是上一根 bar 的值，handle[0] 等于 value。"""
        return self.get(self.keys[0], -i)

    def get(self, key: str, back: int = 0) -> float | None:
        if back < 0 or back >= len(self.values):
            return None
        return self.values[-1 - back].get(key)


# ---------- 官方 1 分钟成交量 ----------


class _MinuteVolumes:
    """某标的某交易日的官方 1 分钟 bar 前缀和，用于给聚合 bar 换上官方成交量。"""

    def __init__(self, rows: list[tuple[int, float, float | None, int | None]]) -> None:
        self.ts = [r[0] for r in rows]
        self.cv, self.cpv, self.cpvv, self.ct, self.cnull = [0.0], [0.0], [0.0], [0], [0]
        for _, v, vw, n in rows:
            self.cv.append(self.cv[-1] + v)
            self.cpv.append(self.cpv[-1] + (vw * v if vw is not None else 0.0))
            self.cpvv.append(self.cpvv[-1] + (v if vw is not None else 0.0))
            self.ct.append(self.ct[-1] + (n or 0))
            self.cnull.append(self.cnull[-1] + (1 if n is None else 0))

    def apply(self, bar: Bar) -> Bar:
        i = bisect.bisect_left(self.ts, bar.ts_start)
        j = bisect.bisect_left(self.ts, bar.ts_end)
        if i == j:
            return bar
        pvv = self.cpvv[j] - self.cpvv[i]
        vwap = (self.cpv[j] - self.cpv[i]) / pvv if pvv > 0 else None
        trades = None if self.cnull[j] - self.cnull[i] else self.ct[j] - self.ct[i]
        return Bar(
            bar.symbol,
            bar.timeframe,
            bar.ts_start,
            bar.open,
            bar.high,
            bar.low,
            bar.close,
            self.cv[j] - self.cv[i],
            vwap,
            trades,
            bar.session,
            bar.closes_at,
        )


# ---------- 结果 ----------


@dataclass
class EngineResult:
    orders: list[Order]
    fills: list[Fill]
    fill_details: dict[str, Any]
    exec_commission: dict[str, Decimal]
    equity: list[tuple[int, float, float, float]]  # (时间, 权益, 现金, 持仓市值)
    marks: list[dict[str, Any]]
    logs: list[dict[str, Any]]
    days: list[date]
    state: dict[str, Any]
    portfolio: Portfolio


@dataclass
class EngineConfig:
    strategy_id: str
    session: SessionFilter = "rth"  # 行情与策略周期覆盖的时段
    trade_extended: bool = False  # 是否允许在盘前盘后开仓（只能用 outside_rth 限价单）
    hold_post: bool = False  # 持仓到盘后、盘后结束前平仓；否则常规时段收盘前平仓
    initial_cash: Decimal = Decimal(100_000)
    decision_delay_ms: int = 0
    bar_grace_ms: int = 300
    whitelist: frozenset[str] = frozenset()
    initial_state: dict[str, Any] = field(default_factory=dict)


class BacktestEngine:
    def __init__(
        self,
        history: HistoryService,
        broker: SimBroker,
        limits: RiskLimits,
        config: EngineConfig,
    ) -> None:
        self.history = history
        self.cal = history.cal
        self.cfg = config
        self.broker = broker
        self.indicators = IndicatorService(history)
        self.grace = config.bar_grace_ms * NS_PER_MS
        self.scheduler = EventScheduler()
        self.clock = SimClock(self.scheduler, 0)
        self.portfolio = Portfolio(config.initial_cash)
        self.oms = OrderManager(
            venue=broker,
            portfolio=self.portfolio,
            limits=limits,
            whitelist=config.whitelist,
            decision_delay_ns=config.decision_delay_ms * NS_PER_MS,
        )
        self.oms.session.allowed_sessions = (
            frozenset({"pre", "regular", "post"})
            if config.trade_extended
            else frozenset({"regular"})
        )
        self.oms.on_notice = self._queue_notice
        self.limits = limits
        self.strategy: Strategy | None = None
        self.ctx = EngineContext(self)
        self._notices: deque[Notice] = deque()
        self._aggs: dict[tuple[str, str], BarAggregator] = {}
        self._handles: dict[tuple[str, str], list[ActiveIndicator]] = {}
        self._bars: dict[tuple[str, str], deque[Bar]] = {}
        self._subs: set[tuple[str, str]] = set()
        self._strategy_subs: set[tuple[str, str]] = set()
        self._minutes: dict[tuple[str, date], _MinuteVolumes] = {}
        self._day: TradingDay | None = None
        self._equity: list[tuple[int, float, float, float]] = []
        self._last_equity_min = -1
        self.marks: list[dict[str, Any]] = []
        self.logs: list[dict[str, Any]] = []
        self._days: list[date] = []
        self._progress: Callable[[int], None] | None = None
        self._feed_end: int | None = None
        self._events: Iterator[BarEvent] = iter(())
        self._next: BarEvent | None = None
        self._started = False
        # 界面等外部观察者：收盘 bar、订单与成交通知
        self.bar_listeners: list[Callable[[Bar], None]] = []
        self.notice_listeners: list[Callable[[Notice], None]] = []

    # ---------- 订阅、指标、历史 ----------

    def subscribe(self, symbol: str, timeframe: str, *, claim: bool = True) -> None:
        """订阅行情周期。策略订阅时占用该标的；界面观看（claim=False）不占用。"""
        check_timeframe(timeframe)
        key = (symbol, timeframe)
        if claim and self.strategy is not None:
            self.oms.claim(symbol, self.cfg.strategy_id)
            self._strategy_subs.add(key)  # 只有策略自己订阅的周期才回调 on_bar
        if key in self._subs:
            return
        new_symbol = all(s != symbol for s, _ in self._subs)
        self._subs.add(key)
        if new_symbol and self._feed_end is not None:
            self._rebuild_feed()
        if timeframe != "1s":
            self._aggs[key] = BarAggregator(symbol, timeframe, self.cal, self.cfg.session)
        # 已收盘的历史 bar（当前时刻之前已可用）
        now = self.clock.now()
        lookback_days = 1 if timeframe_ns(timeframe) < NS_PER_MIN else 30
        hist = self.history.bars(
            symbol, timeframe, now - lookback_days * NS_PER_DAY, now, self.cfg.session
        )
        dq: deque[Bar] = deque(maxlen=HISTORY_KEEP)
        for r in hist.iter_rows(named=True):
            if r["ts_end"] + self.grace <= now:
                dq.append(_row_bar(symbol, timeframe, r))
        self._bars[key] = dq

    def indicator(
        self, name: str, symbol: str, timeframe: str, *, claim: bool = True, **params: Any
    ) -> ActiveIndicator:
        """活动指标实例：用此刻之前已可用的数据预热，之后每根收盘 bar 更新。

        界面观看用 claim=False，不占用标的。
        """
        self.subscribe(symbol, timeframe, claim=claim)
        h = ActiveIndicator(name, params, symbol, timeframe)
        upto = self.clock.now() - self.grace
        for out in self.indicators.warm(h.ind, symbol, timeframe, upto, self.cfg.session):
            h.push(out)
        self._handles.setdefault((symbol, timeframe), []).append(h)
        return h

    # ---------- 主循环 ----------

    def run(
        self,
        strategy_cls: type[Strategy],
        params: StrategyParams,
        start: int,
        end: int,
        progress: Callable[[int], None] | None = None,
    ) -> EngineResult:
        """回测：准备好之后全速推进到区间结束。progress(k)：第 k 个交易日开始时调用。"""
        self.start(start, end, strategy_cls, params, progress=progress)
        self.advance(end + self.grace)
        return self.finish()

    def start(
        self,
        start: int,
        end: int,
        strategy_cls: type[Strategy] | None = None,
        params: StrategyParams | None = None,
        *,
        watch: list[str] | None = None,
        progress: Callable[[int], None] | None = None,
    ) -> None:
        """准备运行区间 [start, end)。回放可以不带策略（纯手动练习），之后再挂载。"""
        days = self.cal.days_overlapping(start, end)
        start = max(start, days[0].start) if days else start
        for d in days:
            self._schedule_day(d, after=start)
        self.clock.advance_to(start)
        self._day = None
        self._progress = progress
        self.ctx.state_obj = StrategyState(self.cfg.initial_state)
        # 当前时刻已经在某个交易日里（例如盘中启动），先完成这一天的开始处理
        loc = self.cal.locate(start)
        if loc is not None and loc[0].start < start:
            self._begin_day(loc[0], start)
        for sym in watch or []:
            self.subscribe(sym, "1s", claim=False)
            self._seed_last_price(sym, start)
        if strategy_cls is not None:
            self.attach_strategy(strategy_cls, params or strategy_cls.Params())
        self._feed_end = end
        self._rebuild_feed()

    def _seed_last_price(self, symbol: str, now: int) -> None:
        """盘中开始时：用此刻之前最后一根已可用的 1 秒 bar 设定最新价（风控和下单需要）。"""
        dq = self._bars.get((symbol, "1s"))
        if dq:
            last = dq[-1]
            self.portfolio.update_mark(symbol, last.close)
            self.oms.last_bar_time[symbol] = last.ts_end + self.grace

    def attach_strategy(self, strategy_cls: type[Strategy], params: StrategyParams) -> None:
        """挂载策略（回测开始时，或回放中途）。指标只用此刻之前已可用的数据预热。

        如果交易日已经开始（盘中挂载），立即补调一次 on_session_open。
        """
        self.strategy = strategy_cls(params, self.ctx)
        self.strategy.on_start()
        if self._day is not None:
            self.strategy.on_session_open()

    def detach_strategy(self) -> None:
        """停止策略：撤销它的挂单，持仓转为手动（DESIGN.md 7.4 人工接管）。"""
        s, self.strategy = self.strategy, None
        self._strategy_subs.clear()
        if s is None:
            return
        s.on_stop()
        for o in self.oms.open_orders(source=self.cfg.strategy_id):
            self.oms.cancel(o.client_order_id, self.clock.now())
        for sym, owner in list(self.oms.owners.items()):
            if owner == self.cfg.strategy_id:
                self.oms.owners[sym] = MANUAL
        self._deliver()

    def _rebuild_feed(self) -> None:
        """按当前订阅的标的（重新）创建行情源，从下一根尚未处理的 1 秒 bar 开始。"""
        from trader.data.replay import HistoricalBarFeed

        assert self._feed_end is not None
        feed = HistoricalBarFeed(self.history, self.cfg.session, decision_delay_ns=self.grace)
        feed.subscribe(sorted({s for s, _ in self._subs}), {"bar_1s"})
        now = self.clock.now()
        # 可用时间晚于当前时刻的 bar：ts_start > now − 宽限 − 1 秒
        self._events = feed.events(now - self.grace - NS_PER_SEC + 1, self._feed_end)
        self._next = next(self._events, None)
        while self._next is not None and self._next.available_time <= now and self._started:
            self._next = next(self._events, None)
        self._started = True

    def next_time(self) -> int | None:
        """下一个要处理的事件时间；没有了返回 None。"""
        t_feed = self._next.available_time if self._next is not None else None
        times = [
            t for t in (t_feed, self.scheduler.peek_time(), self._next_agg_close()) if t is not None
        ]
        return min(times) if times else None

    def advance(self, until: int) -> bool:
        """处理时间不晚于 until 的全部事件，时钟推进到 until。还有后续事件时返回 True。"""
        limit = (self._feed_end if self._feed_end is not None else until) + self.grace
        stop = min(until, limit)
        while True:
            now = self.next_time()
            if now is None or now > stop:
                break
            self.clock.advance_to(now)
            batch: list[BarEvent] = []
            while self._next is not None and self._next.available_time == now:
                batch.append(self._next)
                self._next = next(self._events, None)
            self._step(now, batch)
        if stop > self.clock.now():
            self.clock.advance_to(stop)
        nt = self.next_time()
        return nt is not None and nt <= limit

    def finish(self) -> EngineResult:
        self._flush_aggregators()
        if self.strategy is not None:
            self.strategy.on_stop()
        self._record_equity(self.clock.now(), force=True)
        return self.result()

    def result(self) -> EngineResult:
        return EngineResult(
            orders=list(self.oms.orders.values()),
            fills=list(self.oms.fills),
            fill_details=dict(self.broker.details),
            exec_commission=dict(self.oms.exec_commission),
            equity=self._equity,
            marks=self.marks,
            logs=self.logs,
            days=self._days,
            state=self.ctx.state_obj.snapshot(),
            portfolio=self.portfolio,
        )

    # ---------- 手动下单（回放练习） ----------

    def manual_order(self, intent: OrderIntent) -> Order:
        """界面发出的手动订单：与策略订单走同一条路径（风控、撮合）。来源固定为 manual。"""
        if self.oms.owners.get(intent.symbol) is None:
            self.oms.claim(intent.symbol, MANUAL)
        order = self.oms.submit(intent, self.clock.now())
        self._deliver()
        return order

    def manual_cancel(self, client_order_id: str) -> None:
        self.oms.cancel(client_order_id, self.clock.now())
        self._deliver()

    def _step(self, now: int, batch: list[BarEvent]) -> None:
        # 1. 撮合
        for ev in batch:
            self.oms.handle(self.broker.on_bar(ev.bar))
        # 2. 交付
        self._deliver()
        # 3. 行情：最新价、聚合、封口、指标、on_bar
        for ev in batch:
            bar = ev.bar
            self.portfolio.update_mark(bar.symbol, bar.close)
            self.oms.last_bar_time[bar.symbol] = now
            key = (bar.symbol, "1s")
            if key in self._subs:
                self._publish(bar)
            for (sym, _tf), agg in self._aggs.items():
                if sym == bar.symbol:
                    for closed in agg.update(bar):
                        self._publish(self._with_official_volume(closed))
        self._close_due(now)
        self._deliver()
        # 4. 交易时段事件与定时器
        while (t := self.scheduler.peek_time()) is not None and t == now:
            ev = self.scheduler.pop()
            if isinstance(ev, SessionEvent):
                self._on_session(ev)
            elif isinstance(ev, TimerEvent):
                self._on_timer(ev)
            self._deliver()
        self._record_equity(now)

    def _next_agg_close(self) -> int | None:
        times = [
            t + self.grace for a in self._aggs.values() if (t := a.next_close_time()) is not None
        ]
        return min(times) if times else None

    def _close_due(self, now: int) -> None:
        for agg in self._aggs.values():
            t = agg.next_close_time()
            if t is not None and t + self.grace <= now:
                closed = agg.on_time(t)
                if closed is not None:
                    self._publish(self._with_official_volume(closed))

    def _flush_aggregators(self) -> None:
        for agg in self._aggs.values():
            agg.flush()  # 区间结束时未收盘的 bar 不交给策略

    def _with_official_volume(self, bar: Bar) -> Bar:
        if timeframe_ns(bar.timeframe) < NS_PER_MIN:
            return bar
        day = self.cal.trading_date_of(bar.ts_start)
        if day is None:
            return bar
        key = (bar.symbol, day)
        mv = self._minutes.get(key)
        if mv is None:
            if self.history.catalog.fingerprint(bar.symbol, day, kind="bar_1m") is None:
                mv = _MinuteVolumes([])
            else:
                df = self.history.minute_store.read_day(bar.symbol, day)
                mv = _MinuteVolumes(
                    list(df.select("ts_start", "volume", "vwap", "trades").iter_rows())
                )
            self._minutes[key] = mv
        return mv.apply(bar) if mv.ts else bar

    def _publish(self, bar: Bar) -> None:
        key = (bar.symbol, bar.timeframe)
        self._bars.setdefault(key, deque(maxlen=HISTORY_KEEP)).append(bar)
        for h in self._handles.get(key, []):
            h.push(h.ind.update(bar), bar.ts_start)
        for listener in self.bar_listeners:
            listener(bar)
        if self.strategy is not None and key in self._strategy_subs:
            self.strategy.on_bar(bar)

    def partial(self, symbol: str, timeframe: str) -> Bar | None:
        """正在形成的 bar（closed=False 的中间状态，仅供界面）。"""
        agg = self._aggs.get((symbol, timeframe))
        return agg.partial() if agg is not None else None

    # ---------- 交易日与时段 ----------

    def _schedule_day(self, day: TradingDay, after: int) -> None:
        """排入交易时段事件和收盘前平仓；盘中开始时，已经过去的事件不排。"""
        for ev in day.session_events():
            if ev.ts >= after:
                self.scheduler.push(ev)
        flat = self._flatten_at(day)
        if flat >= after:
            self.scheduler.push(TimerEvent(_FLATTEN, flat), phase=Phase.SESSION)

    def _flatten_at(self, day: TradingDay) -> int:
        end = day.post_close if self.cfg.hold_post else day.close
        return end - self.limits.flatten_before_close_min * NS_PER_MIN

    def _begin_day(self, day: TradingDay, now: int) -> None:
        self._day = day
        self._days.append(day.day)
        if self._progress is not None:
            self._progress(len(self._days) - 1)
        ss = self.oms.session
        ss.flattening = False
        ss.regular_open = day.open
        ss.flatten_at = self._flatten_at(day)
        ss.day_start_equity = self.portfolio.equity()
        ss.session = day.session_of(now)
        for hs in self._handles.values():
            for h in hs:
                if h.ind.scope == "session":
                    h.ind.on_session_open(day)

    def _on_session(self, ev: SessionEvent) -> None:
        s = self.strategy
        loc = self.cal.locate(ev.ts)
        ss = self.oms.session
        first = (ev.kind == SessionKind.OVERNIGHT_OPEN) or (
            ev.kind == SessionKind.PRE_OPEN and (loc is None or not loc[0].has_overnight)
        )
        if first and loc is not None:
            if self._day is None or self._day.day != loc[0].day:
                self._begin_day(loc[0], ev.ts)
                if s is not None:
                    s.on_session_open()
            return
        if ev.kind == SessionKind.PRE_OPEN:
            ss.session = "pre"
        elif ev.kind == SessionKind.OPEN:
            ss.session = "regular"
        elif ev.kind == SessionKind.CLOSE:
            ss.session = "post"
            self.oms.handle(self.broker.expire(ev.ts, outside_rth_too=False))
            if s is not None:
                s.on_session_close()
        elif ev.kind == SessionKind.POST_CLOSE:
            ss.session = None
            self.oms.handle(self.broker.expire(ev.ts, outside_rth_too=True))
            open_pos = {s: p.qty for s, p in self.portfolio.positions.items() if p.qty}
            if open_pos:
                self.logs.append(
                    {"ts": ev.ts, "level": "warning", "msg": f"收盘后仍有持仓：{open_pos}"}
                )

    def _on_timer(self, ev: TimerEvent) -> None:
        if ev.name == _FLATTEN:
            self.oms.flatten(ev.at, list(self.oms.owners), outside_rth=self.cfg.hold_post)
            return
        if self.strategy is not None:
            self.strategy.on_timer(ev.name)

    # ---------- 回调交付 ----------

    def _queue_notice(self, n: Notice) -> None:
        self._notices.append(n)

    def _deliver(self) -> None:
        s = self.strategy
        while self._notices:
            n = self._notices.popleft()
            for listener in self.notice_listeners:
                listener(n)
            if s is None or n.order.intent.source != self.cfg.strategy_id:
                continue
            if n.kind == "fill" and n.fill is not None:
                s.on_fill(n.fill)
            else:
                s.on_order(n.order)

    def _record_equity(self, now: int, force: bool = False) -> None:
        minute = now // NS_PER_MIN
        if minute == self._last_equity_min and not force:
            return
        self._last_equity_min = minute
        p = self.portfolio
        mv = sum(p.market_value(s) for s in p.positions)
        self._equity.append((minute * NS_PER_MIN, p.equity(), float(p.cash), mv))


def _row_bar(symbol: str, timeframe: str, r: dict[str, Any]) -> Bar:
    return Bar(
        symbol,
        timeframe,
        r["ts_start"],
        r["open"],
        r["high"],
        r["low"],
        r["close"],
        r["volume"],
        r["vwap"],
        r["trades"],
        r["session"],
        r["ts_end"],
    )


class EngineContext:
    """策略看到的 ctx（DESIGN.md 7.2）。不提供查询运行模式的方法。"""

    def __init__(self, engine: BacktestEngine) -> None:
        self._e = engine
        self.state_obj = StrategyState()

    # 行情与指标
    def subscribe(self, symbol: str, timeframe: str) -> None:
        self._e.subscribe(symbol, timeframe)

    def indicator(self, name: str, symbol: str, timeframe: str, **params: Any) -> ActiveIndicator:
        return self._e.indicator(name, symbol, timeframe, **params)

    def history(self, symbol: str, timeframe: str, n: int) -> list[Bar]:
        dq = self._e._bars.get((symbol, timeframe))
        if dq is None:
            self._e.subscribe(symbol, timeframe)
            dq = self._e._bars[(symbol, timeframe)]
        return list(dq)[-n:] if n > 0 else []

    # 下单
    def _order(
        self,
        side: str,
        symbol: str,
        qty: int,
        reason: Reason,
        order_type: str,
        limit_price: Price,
        stop_price: Price,
        take_profit: Price,
        stop_loss: Price,
        tif: str,
        outside_rth: bool,
    ) -> str:
        def px(x: Price) -> Decimal | None:
            return None if x is None else round_price(x)

        intent = OrderIntent(
            source=self._e.cfg.strategy_id,
            symbol=symbol,
            side=side,  # type: ignore[arg-type]
            qty=qty,
            order_type=order_type,  # type: ignore[arg-type]
            reason=reason,
            limit_price=px(limit_price),
            stop_price=px(stop_price),
            tif=tif,  # type: ignore[arg-type]
            outside_rth=outside_rth,
            take_profit=px(take_profit),
            stop_loss=px(stop_loss),
        )
        return self._e.oms.submit(intent, self._e.clock.now()).client_order_id

    def buy(
        self,
        symbol: str,
        qty: int,
        *,
        reason: Reason,
        order_type: str = "MKT",
        limit_price: Price = None,
        stop_price: Price = None,
        take_profit: Price = None,
        stop_loss: Price = None,
        tif: str = "DAY",
        outside_rth: bool = False,
    ) -> str:
        return self._order(
            "BUY", symbol, qty, reason, order_type, limit_price, stop_price,
            take_profit, stop_loss, tif, outside_rth,
        )  # fmt: skip

    def sell(
        self,
        symbol: str,
        qty: int,
        *,
        reason: Reason,
        order_type: str = "MKT",
        limit_price: Price = None,
        stop_price: Price = None,
        take_profit: Price = None,
        stop_loss: Price = None,
        tif: str = "DAY",
        outside_rth: bool = False,
    ) -> str:
        return self._order(
            "SELL", symbol, qty, reason, order_type, limit_price, stop_price,
            take_profit, stop_loss, tif, outside_rth,
        )  # fmt: skip

    def modify(
        self,
        client_order_id: str,
        *,
        reason: Reason,
        qty: int | None = None,
        limit_price: Price = None,
        stop_price: Price = None,
    ) -> None:
        rej = self._e.oms.modify(
            client_order_id,
            self._e.clock.now(),
            qty=qty,
            limit_price=None if limit_price is None else round_price(limit_price),
            stop_price=None if stop_price is None else round_price(stop_price),
        )
        self.log(
            "改单" + ("被拒绝" if rej else ""),
            order=client_order_id,
            reason=reason.code,
            **({"rule": rej.rule, "detail": rej.detail} if rej else {}),
        )

    def cancel(self, client_order_id: str, *, reason: Reason) -> None:
        self.log("撤单", order=client_order_id, reason=reason.code)
        self._e.oms.cancel(client_order_id, self._e.clock.now())

    def close_position(self, symbol: str, *, reason: Reason) -> str | None:
        """平掉该标的全部持仓：先撤销该标的的挂单，再发市价单（盘前盘后发让价 1% 的限价单）。"""
        e = self._e
        for o in e.oms.open_orders(symbol, e.cfg.strategy_id):
            e.oms.cancel(o.client_order_id, e.clock.now())
        pos = e.portfolio.position(symbol)
        if pos.qty == 0:
            return None
        side = "SELL" if pos.qty > 0 else "BUY"
        regular = e.oms.session.session == "regular"
        last = e.portfolio.last_price.get(symbol, float(pos.avg_cost))
        return self._order(
            side,
            symbol,
            abs(pos.qty),
            reason,
            "MKT" if regular else "LMT",
            None if regular else last * (0.99 if side == "SELL" else 1.01),
            None,
            None,
            None,
            "DAY",
            not regular,
        )

    # 状态
    def position(self, symbol: str) -> Position:
        return self._e.portfolio.position(symbol)

    def open_orders(self, symbol: str | None = None) -> list[Order]:
        return self._e.oms.open_orders(symbol, self._e.cfg.strategy_id)

    def now(self) -> int:
        return self._e.clock.now()

    def set_timer(self, name: str, at: int) -> None:
        if name.startswith("__"):
            raise ValueError("定时器名不能以 __ 开头（保留给框架）")
        self._e.clock.set_timer(name, at)

    @property
    def state(self) -> StrategyState:
        return self.state_obj

    @property
    def today(self) -> TradingDay | None:
        return self._e._day

    # 记录
    def log(self, msg: str, **fields: Any) -> None:
        self._e.logs.append({"ts": self._e.clock.now(), "level": "info", "msg": msg, **fields})

    def mark(self, symbol: str, text: str, kind: str = "note") -> None:
        self._e.marks.append(
            {"ts": self._e.clock.now(), "symbol": symbol, "text": text, "kind": kind}
        )
