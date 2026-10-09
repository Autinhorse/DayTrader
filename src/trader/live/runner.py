"""实盘版运行器（DESIGN.md 5.5、8.5、9.4、12.3、12.4）。两种模拟方式：

- local_paper：IBKR 实时行情 + 本地模拟撮合（SimBroker），不向券商发送任何订单（连接用只读模式）；
- broker_paper：IBKR 实时行情 + IBKR 模拟账户执行（IbkrExecutor，原生括号单），定期与券商对账。

主循环每 100 毫秒：取本机时间 → 券商回报归约 → 行情源封口 1 秒 bar → 送入引擎并推进到当前时刻 →
系统事件（断线、行情中断、缺口）暂停相关策略 → 订单和成交落盘 → 落盘成功后发出请求。

对账（broker_paper）：每分钟比较本地持仓与券商持仓、券商挂单里有没有不认识的订单；连续两次不一致
就发 RECONCILE_MISMATCH、暂停策略、等人工处理，不自动修正。人工可以“按券商持仓重置”。

启动检查（任何一项不通过就拒绝启动）：配置合法（风控阈值全部显式、只允许模拟端口）、
账户号是模拟账户（DU 开头）、本机与 IB 服务器时间偏差在阈值内、按固定顺序完成订阅和补数。
策略启动后默认暂停：必须人工开启（命令行 --start-strategy 或界面按钮），开启前检查行情能力。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from trader.brokers.ibkr.api import IbApi, SafetyError, check_paper_accounts
from trader.brokers.ibkr.executor import IbkrExecutor
from trader.brokers.ibkr.feed import FeedSettings, IbkrFeed
from trader.brokers.sim.matcher import SimBroker
from trader.config import Universe
from trader.core.events import SystemEvent, SystemKind
from trader.core.models import CommissionUpdate, Order
from trader.core.timeutil import NS_PER_DAY, NS_PER_MS, NS_PER_SEC, from_ns, timeframe_ns
from trader.core.trading_calendar import TradingCalendar
from trader.data.catalog import Catalog
from trader.data.live_history import LiveHistory
from trader.engine.engine import MANUAL, BacktestEngine, EngineConfig
from trader.live.config import LiveConfig
from trader.oms.journal import Journal, recover_into
from trader.oms.manager import Notice
from trader.oms.orders import QueryResult
from trader.strategy.base import Strategy, get_strategy

LOOP_MS = 100


class StartupError(RuntimeError):
    pass


@dataclass
class Alert:
    ts: int
    kind: str
    detail: str
    symbol: str | None = None


@dataclass
class LiveRunner:
    cfg: LiveConfig
    api: IbApi
    universe: Universe
    data_dir: Path
    run_dir: Path
    now: Callable[[], int]
    cal: TradingCalendar = field(default_factory=TradingCalendar)
    alerts: list[Alert] = field(default_factory=list)
    engine: BacktestEngine | None = None
    feed: IbkrFeed | None = None
    journal: Journal | None = None
    strategy_cls: type[Strategy] | None = None
    strategy_running: bool = False
    on_alert: Callable[[Alert], None] | None = None
    _changed: set[str] = field(default_factory=set)
    _fills_saved: int = 0
    _comm_saved: int = 0
    _stop: bool = False
    _reconnecting: bool = False
    startup_bars: int = 0
    clock_sample_gap_s: float = 1.1
    executor: IbkrExecutor | None = None
    account: str = ""
    _mismatch: dict[str, str] = field(default_factory=dict)  # 上一次对账发现的不一致
    _last_reconcile: int = 0
    _tasks: set[asyncio.Future] = field(default_factory=set)
    feed_timing: tuple[float, float] = (
        3.0,
        0.3,
    )  # 订阅后等待补数的秒数、补数请求间隔（测试改为 0）

    @property
    def strategy_id(self) -> str:
        return self.cfg.strategy.name if self.cfg.strategy else MANUAL

    # ---------- 启动 ----------

    async def startup(self) -> None:
        cfg = self.cfg
        if cfg.profile not in ("local_paper", "broker_paper"):
            raise StartupError(f"阶段 6 不支持运行方式 {cfg.profile}")
        broker = cfg.profile == "broker_paper"
        now = self.now()
        today = self._today(now)
        self._alert(now, "startup", f"{cfg.profile} 启动，交易日 {today}")
        # 1. 连接（local_paper 只读，不可能下单；broker_paper 可下单）+ 账户检查
        await self.api.connect(cfg.ibkr.host, cfg.ibkr.port, cfg.ibkr.client_id, not broker)
        try:
            accounts = self.api.managed_accounts()
            check_paper_accounts(accounts)
            if cfg.ibkr.account and cfg.ibkr.account not in accounts:
                raise SafetyError(f"配置的账户 {cfg.ibkr.account} 不在已登录的账户 {accounts} 里")
            if broker and not cfg.ibkr.account and len(accounts) != 1:
                raise SafetyError(f"登录了多个账户 {accounts}，请在配置 ibkr.account 里指定一个")
            self.account = cfg.ibkr.account or accounts[0]
        except SafetyError:
            self.api.disconnect()
            raise
        mode = "可下单" if broker else "只读"
        self._alert(now, "startup", f"已连接 IB（账户 {self.account}，{mode}）")
        # 2. 时钟偏差
        skew = await self._clock_skew()
        if skew > cfg.max_clock_skew_ms * NS_PER_MS:
            self.api.disconnect()
            raise StartupError(
                f"本机时间与 IB 服务器相差 {skew / 1e9:.1f} 秒，超过 {cfg.max_clock_skew_ms} 毫秒；"
                "请先同步 Windows 时间（设置 → 时间和语言 → 立即同步）"
            )
        # 3. 本地历史数据是否更新到上一个交易日
        catalog = Catalog(self.data_dir / "catalog.sqlite")
        symbols = cfg.symbols or self.universe.names
        prev = self.cal.previous_trading_day(today)
        stale = [s for s in symbols if prev not in set(catalog.known_days(s))]
        if stale:
            self._alert(
                now, "data_stale",
                f"本地数据没有 {prev} 的 1 秒 bar：{stale}。指标预热可能不完整，"
                "请先运行 uv run trader data update",
            )  # fmt: skip
        # 4. 订阅 → 补数 → 交接
        history = LiveHistory(self.data_dir, self.cal, catalog, today)
        tick = [s.symbol for s in self.universe.symbols if s.feed == "tick" and s.symbol in symbols]
        snap = [s for s in symbols if s not in tick]
        settings = FeedSettings(
            tick, snap, tick_limit=cfg.feed.tick_limit, grace_ms=cfg.feed.grace_ms,
            watchdog_regular_s=cfg.feed.watchdog_regular_s,
            watchdog_extended_s=cfg.feed.watchdog_extended_s,
            settle_s=self.feed_timing[0], request_gap_s=self.feed_timing[1],
        )  # fmt: skip
        record = (
            self.data_dir / "live" / today.isoformat() / cfg.profile if cfg.feed.record else None
        )
        self.feed = IbkrFeed(self.api, self.cal, history, settings, self.now, record)
        self._alert(self.now(), "startup", f"订阅 {len(tick)} 个逐笔、{len(snap)} 个快照，补数中…")
        for ev in await self.feed.start():
            self._system(ev)
        # 5. 引擎（实时模式）+ 执行器（模拟撮合或 IBKR）
        sim = cfg.sim.model_copy(update={"bar_grace_ms": cfg.feed.grace_ms})
        if broker:
            self.executor = IbkrExecutor(self.api, self.now, self.account)
            self.executor.bind()
        self.engine = BacktestEngine(
            history,
            self.executor if self.executor is not None else SimBroker(sim),
            cfg.risk,
            EngineConfig(
                strategy_id=self.strategy_id,
                session=cfg.session,
                trade_extended=cfg.trade_extended,
                hold_post=cfg.hold_post,
                initial_cash=Decimal(str(cfg.initial_cash)),
                bar_grace_ms=cfg.feed.grace_ms,
                whitelist=frozenset(symbols),
            ),
        )
        e = self.engine
        e.oms.deferred = True
        e.oms.native_brackets = broker
        e.oms.on_system = lambda ts, ref, msg: self._alert(ts, "order", msg, None)
        e.notice_listeners.append(self._on_notice)
        e.on_new_symbol = self._on_new_symbol
        start = self.now()
        e.start(start, start + NS_PER_DAY, watch=list(self.feed.symbols), live=True)
        # 6. 恢复当天的订单、成交和策略状态
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.journal = Journal(self.run_dir / "journal.sqlite", self.run_dir / "events.jsonl")
        rec = self.journal.load()
        if rec.orders:
            to_query = recover_into(e.oms, rec, start)
            if self.executor is not None:
                # 向券商查询：挂单状态、断线或崩溃期间的成交；UNKNOWN 的订单给出结论
                self.executor.adopt(list(e.oms.orders.values()))
                await self.executor.sync(query=to_query)
                e.handle_updates(self.executor.drain())
            else:
                # 本地模拟撮合不保存挂单：恢复后查询结果都是“没有这张订单”
                e.oms.handle([QueryResult(c, start, found=False) for c in to_query])
            e.cfg.initial_state = rec.states.get(self.strategy_id, {})
            self._alert(
                start, "recover", f"从日志恢复 {len(rec.orders)} 张订单，持仓 {self.positions()}"
            )
            self._fills_saved = len(e.oms.fills)
            self._comm_saved = len(e.oms.commission_log)
        if self.cfg.strategy is not None:
            self.strategy_cls = get_strategy(self.cfg.strategy.name)
        # 补数期间实时数据已经在缓冲：先处理掉（策略此时尚未开启），不算作运行中的迟到
        self.step(self.now())
        if self.executor is not None:
            await self.reconcile()
        self.startup_bars, e.late_bars = e.late_bars, 0
        self._alert(
            self.now(), "startup",
            f"启动完成（补数期间缓冲的 {self.startup_bars} 根 1 秒 bar 已处理）；"
            "策略处于暂停状态，需要人工开启",
        )  # fmt: skip

    async def _clock_skew(self) -> int:
        """本机与 IB 服务器的时间偏差下限（纳秒）。

        IB 的服务器时间只精确到秒（截断），真实时间在 [t, t+1 秒) 内；本机时间取请求前后的中点，
        再扣掉往返时间的一半。返回与这些不确定性相容的最小偏差，只有确实超出才拒绝启动。
        """
        best = None
        for i in range(3):
            if i:
                await asyncio.sleep(self.clock_sample_gap_s)  # IB 不回应连续的时间请求
            t0 = self.now()
            try:
                server = await self.api.server_time()
            except TimeoutError:
                continue
            t1 = self.now()
            mid, half_rtt = (t0 + t1) // 2, (t1 - t0) // 2
            lo, hi = server - mid - half_rtt, server + NS_PER_SEC - mid + half_rtt
            skew = 0 if lo <= 0 <= hi else min(abs(lo), abs(hi))
            best = skew if best is None else min(best, skew)
        if best is None:
            raise StartupError("取不到 IB 服务器时间，无法核对本机时钟")
        return best

    def _today(self, now: int) -> date:
        loc = self.cal.locate(now)
        if loc is not None:
            return loc[0].day
        d = from_ns(now).date()
        return d if self.cal.is_trading_day(d) else self.cal.next_trading_day(d)

    # ---------- 策略控制（人工） ----------

    def start_strategy(self, force: bool = False) -> str | None:
        """开启策略。返回拒绝原因；None 表示已开启。"""
        e, cls, spec = self.engine, self.strategy_cls, self.cfg.strategy
        if e is None or cls is None or spec is None:
            return "没有配置策略"
        if self.strategy_running:
            e.oms.halted.discard(self.strategy_id)
            return None
        try:
            e.attach_strategy(cls, cls.Params(**spec.params))
        except ValueError as exc:  # 例如标的已被手动持仓占用
            e.detach_strategy()
            return f"策略没有开启：{exc}"
        reason = self._check_requirements(cls, force)
        if reason is not None:
            e.detach_strategy()
            return reason
        self.strategy_running = True
        self._alert(self.now(), "strategy", f"策略 {spec.name} 已开启")
        return None

    def _check_requirements(self, cls: type[Strategy], force: bool) -> str | None:
        assert self.engine is not None and self.feed is not None
        req = cls.requires
        claimed = [s for s, o in self.engine.oms.owners.items() if o == self.strategy_id]
        for sym in claimed:
            if sym not in self.feed.symbols:
                return f"{sym} 没有实时行情"
            caps = self.feed.capabilities(sym)
            degraded = (req.trades_complete and not caps.trades_complete) or (
                timeframe_ns(req.min_bar_interval) < timeframe_ns(caps.min_bar_interval)
            )
            if degraded and not req.allow_degraded:
                need = (
                    "完整逐笔、" if req.trades_complete else ""
                ) + f"最小周期 {req.min_bar_interval}"
                have = "逐笔" if caps.trades_complete else "快照"
                return (
                    f"{sym} 的行情能力不满足策略要求（{cls.name} 需要{need}；{sym} 是{have}行情）"
                )
            if self.feed.symbols[sym].gap and not force:
                return f"{sym} 当天数据有缺口，确认后用“强制开启”"
        return None

    def pause_strategy(self, why: str = "人工暂停") -> None:
        """暂停：保留持仓和挂单，策略不能再开新仓（减仓、止损照常）。"""
        e = self.engine
        if e is not None and self.strategy_running and self.strategy_id not in e.oms.halted:
            e.oms.halted.add(self.strategy_id)
            self._alert(self.now(), "strategy", f"策略已暂停：{why}")

    def stop_strategy(self) -> None:
        """停止：撤销策略的挂单，持仓转为手动（不自动平仓）。"""
        if self.engine is not None and self.strategy_running:
            self.engine.detach_strategy()
            self.engine.oms.halted.discard(self.strategy_id)
            self.strategy_running = False
            self._alert(self.now(), "strategy", "策略已停止，持仓转为手动")

    # 四个风控按钮（DESIGN.md 11）
    def halt_new(self, on: bool = True) -> None:
        if self.engine is not None:
            self.engine.oms.halt_all_new = on
            self._alert(self.now(), "risk", "停止新开仓" if on else "恢复新开仓")

    def cancel_all(self) -> None:
        if self.engine is not None:
            for o in self.engine.oms.open_orders():
                self.engine.manual_cancel(o.client_order_id)
            self._alert(self.now(), "risk", "已撤销全部挂单")

    def flatten_all(self) -> None:
        if self.engine is not None:
            e = self.engine
            syms = [s for s, p in e.portfolio.positions.items() if p.qty != 0]
            syms += [o.intent.symbol for o in e.oms.open_orders()]
            e.oms.flatten(e.clock.now(), sorted(set(syms)), code="manual_flatten",
                          outside_rth=e.oms.session.session != "regular")  # fmt: skip
            self._alert(self.now(), "risk", f"人工平仓：{sorted(set(syms))}")

    # ---------- 主循环 ----------

    async def run(self, until: int | None = None) -> None:
        assert self.engine is not None and self.feed is not None
        day = self.cal.trading_day(self.engine.history.today)  # type: ignore[attr-defined]
        until = day.end if until is None else until
        last_status = 0
        while not self._stop and self.now() < until:
            self.step(self.now())
            if self.executor is not None and self.now() - self._last_reconcile >= 60 * NS_PER_SEC:
                self._last_reconcile = self.now()
                self._spawn(self.reconcile())
            if self.now() - last_status >= 60 * NS_PER_SEC:
                last_status = self.now()
                self._write_status()
            await asyncio.sleep(LOOP_MS / 1000)
            if not self.feed.connected and not self._reconnecting:
                asyncio.ensure_future(self._reconnect())
        self.step(self.now())
        self._write_status()

    def step(self, now: int) -> None:
        e, feed, ex = self.engine, self.feed, self.executor
        assert e is not None and feed is not None
        if ex is not None:
            e.handle_updates(ex.drain())
        e.push(feed.poll(now))
        e.advance(now)
        for ev in feed.drain_system():
            self._system(ev)
        self._persist(now)
        if ex is not None:
            ex.pump(now)
            self._changed.update(ex.assigned)
            ex.assigned.clear()
            for t, ref, msg in ex.messages:
                self._alert(t, "broker", f"{ref} {msg}".strip())
            ex.messages.clear()
            timed_out = ex.take_timed_out()
            if timed_out:
                self._alert(now, "broker", f"发送超时，向券商查询：{timed_out}")
                self._spawn(ex.sync(query=timed_out))
        feed.flush()

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---------- 对账（DESIGN.md 8.5） ----------

    async def reconcile(self) -> dict[str, str]:
        """比较本地持仓与券商持仓、券商的挂单。连续两次不一致才报警（避免成交在途的瞬时差异）。

        返回本次发现的不一致（标的或订单 → 说明）。
        """
        e, ex = self.engine, self.executor
        if e is None or ex is None:
            return {}
        self._last_reconcile = self.now()
        broker_pos = {
            p.symbol: int(round(p.qty))
            for p in await self.api.positions()
            if p.account == self.account and p.qty
        }
        local = {s: p.qty for s, p in e.portfolio.positions.items() if p.qty}
        found: dict[str, str] = {}
        for sym in sorted(set(broker_pos) | set(local)):
            b, mine = broker_pos.get(sym, 0), local.get(sym, 0)
            if b != mine:
                found[sym] = f"本地持仓 {mine}，券商持仓 {b}"
        for oid, st in ex.foreign.items():
            if st.status not in ("Cancelled", "ApiCancelled", "Filled", "Inactive"):
                found[f"order:{oid}"] = f"券商有不认识的挂单 {st.symbol}（orderRef={st.order_ref}）"
        confirmed = {k: v for k, v in found.items() if self._mismatch.get(k) == v}
        self._mismatch = found
        if confirmed:
            detail = "；".join(f"{k}: {v}" for k, v in confirmed.items())
            self._system(SystemEvent(SystemKind.RECONCILE_MISMATCH, self.now(), detail))
        return found

    def resync_to_broker(self, positions: dict[str, tuple[int, float]]) -> None:
        """人工操作“按券商持仓重置”：本地持仓改为券商的数字，归属转为手动（不下任何单）。"""
        e = self.engine
        if e is None:
            return
        for sym in set(positions) | {s for s, p in e.portfolio.positions.items() if p.qty}:
            qty, cost = positions.get(sym, (0, 0.0))
            e.portfolio.set_position(sym, qty, Decimal(str(cost)))
            if qty and sym not in e.oms.owners:
                e.oms.owners[sym] = MANUAL
        self._mismatch.clear()
        self._alert(self.now(), "reconcile", f"已按券商持仓重置：{positions}")

    def stop(self) -> None:
        self._stop = True

    async def _reconnect(self) -> None:
        assert self.feed is not None
        self._reconnecting = True
        try:
            while not self._stop:
                await asyncio.sleep(5)
                if not self.api.is_connected():
                    try:
                        await self.api.connect(
                            self.cfg.ibkr.host, self.cfg.ibkr.port, self.cfg.ibkr.client_id, True
                        )
                        check_paper_accounts(self.api.managed_accounts())
                    except SafetyError:
                        self.api.disconnect()
                        raise
                    except Exception as exc:  # noqa: BLE001 - Gateway 未就绪时继续重试
                        self._alert(self.now(), "connection", f"重连失败：{exc}")
                        continue
                await self.feed.resync()
                self._alert(self.now(), "connection", "已重连并补数；策略保持暂停，需要人工恢复")
                return
        finally:
            self._reconnecting = False

    # ---------- 系统事件 ----------

    def _system(self, ev: SystemEvent) -> None:
        self._alert(ev.ts, ev.kind.value, ev.detail, ev.symbol)
        if ev.kind in (SystemKind.CONNECTION_LOST, SystemKind.RECONCILE_MISMATCH):
            self.pause_strategy(ev.detail)
        elif ev.kind in (SystemKind.FEED_INTERRUPTED, SystemKind.FEED_DEGRADED):
            owner = self.engine.oms.owners.get(ev.symbol or "") if self.engine else None
            if owner == self.strategy_id:
                self.pause_strategy(f"{ev.symbol}：{ev.detail}")

    def _alert(self, ts: int, kind: str, detail: str, symbol: str | None = None) -> None:
        a = Alert(ts, kind, detail, symbol)
        self.alerts.append(a)
        if self.on_alert is not None:
            self.on_alert(a)

    def _on_new_symbol(self, symbol: str) -> None:
        if self.feed is not None and symbol not in self.feed.symbols:
            asyncio.ensure_future(self.feed.add_symbol(symbol))

    def _on_notice(self, n: Notice) -> None:
        self._changed.add(n.order.client_order_id)

    # ---------- 落盘 ----------

    def _persist(self, now: int) -> None:
        e, j = self.engine, self.journal
        assert e is not None and j is not None
        oms = e.oms
        changed = self._changed | {o.client_order_id for _, o, _, _ in oms.outbox}
        new_fills = oms.fills[self._fills_saved :]
        new_comm: list[CommissionUpdate] = oms.commission_log[self._comm_saved :]
        if changed or new_fills or new_comm:
            orders: list[Order] = [oms.orders[c] for c in changed if c in oms.orders]
            states = (
                {self.strategy_id: e.ctx.state_obj.snapshot()} if self.strategy_running else None
            )
            j.commit(now, orders=orders, fills=new_fills, commissions=new_comm, owners=oms.owners,
                     states=states)  # fmt: skip
            self._changed.clear()
            self._fills_saved += len(new_fills)
            self._comm_saved += len(new_comm)
        oms.flush()  # 落盘之后才发出请求；发出时同步得到的回报（模拟撮合）在下一轮落盘

    # ---------- 状态 ----------

    def positions(self) -> dict[str, int]:
        if self.engine is None:
            return {}
        return {s: p.qty for s, p in self.engine.portfolio.positions.items() if p.qty}

    def status(self) -> dict[str, Any]:
        """完整状态快照（界面每秒刷新一次；也写入 status.json）。只含可 JSON 序列化的值。"""
        e, feed = self.engine, self.feed
        out: dict[str, Any] = {
            "profile": self.cfg.profile,
            "ts": self.now(),
            "ready": e is not None,
        }
        if e is None or feed is None:
            return out
        p = e.portfolio
        orders = sorted(e.oms.orders.values(), key=lambda o: o.created_at)[-200:]
        out |= {
            "connected": feed.connected,
            "account": self.account,
            "session": e.oms.session.session,
            "strategy": self.cfg.strategy.name if self.cfg.strategy else None,
            "strategy_running": self.strategy_running,
            "strategy_paused": self.strategy_id in e.oms.halted,
            "halt_all_new": e.oms.halt_all_new,
            "flattening": e.oms.session.flattening,
            "equity": p.equity(),
            "cash": float(p.cash),
            "day_pnl": p.equity() - e.oms.session.day_start_equity,
            "realized": float(p.realized_pnl()),
            "unrealized": p.unrealized_pnl(),
            "commission": float(p.commission),
            "positions": {
                s: {
                    "qty": x.qty,
                    "avg_cost": float(x.avg_cost),
                    "last": p.last_price.get(s),
                    "unrealized": x.unrealized_pnl,
                    "owner": e.oms.owners.get(s, ""),
                }
                for s, x in p.positions.items()
                if x.qty
            },
            "orders": [_order_row(o) for o in orders],
            "open_orders": [_order_row(o) for o in e.oms.open_orders()],
            "fills": [
                {
                    "ts": f.ts,
                    "id": f.client_order_id,
                    "symbol": e.oms.orders[f.client_order_id].intent.symbol,
                    "side": e.oms.orders[f.client_order_id].intent.side,
                    "qty": f.qty,
                    "price": float(f.price),
                    "exec_id": f.broker_exec_id,
                }
                for f in e.oms.fills[-100:]
                if f.client_order_id in e.oms.orders
            ],
            "queued": self.executor.queued if self.executor else 0,
            "mismatch": dict(self._mismatch),
            "late_bars": e.late_bars,
            "late_revisions": feed.revisions,
            "stale": [s for s, st in feed.symbols.items() if st.stale],
            "gaps": [s for s, st in feed.symbols.items() if st.gap],
            "symbols": sorted(feed.symbols),
            "last_price": dict(p.last_price),
            "last_bar": {
                s: from_ns(t).strftime("%H:%M:%S") for s, t in sorted(e.oms.last_bar_time.items())
            },
        }
        return out

    def _write_status(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "status.json").write_text(
            json.dumps(self.status(), ensure_ascii=False, indent=1, default=str), encoding="utf-8"
        )

    def close(self) -> None:
        if self.feed is not None:
            self.feed.close()
        if self.journal is not None:
            self.journal.close()
        if self.api.is_connected():
            self.api.disconnect()


def _order_row(o: Order) -> dict[str, Any]:
    it = o.intent
    return {
        "id": o.client_order_id,
        "created": o.created_at,
        "source": it.source,
        "symbol": it.symbol,
        "side": it.side,
        "qty": it.qty,
        "type": it.order_type,
        "limit": float(it.limit_price) if it.limit_price is not None else None,
        "stop": float(it.stop_price) if it.stop_price is not None else None,
        "filled": o.filled_qty,
        "avg": float(o.avg_fill_price) if o.avg_fill_price is not None else None,
        "status": o.status.value,
        "role": o.role,
        "parent": o.parent_id,
        "reason": it.reason.code,
        "reject": f"{o.reject_rule}: {o.reject_detail}" if o.reject_rule else "",
        "broker_id": o.broker_order_id,
    }
