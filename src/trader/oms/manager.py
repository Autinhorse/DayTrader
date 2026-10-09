"""订单管理（DESIGN.md 8.2）：生成订单号、风控、交给执行器、按回报归约订单并更新持仓。

括号单有两种方式：
- **本地管理**（回测、回放、local_paper）：入场单成交后由这里生成止盈（限价）和
  止损（止损单）两张子单，同属一个 OCA 组；
子单数量始终等于"入场单累计成交量 − 已成交的退出量"，一张退出单成交后另一张随之缩减或撤销，
不会超量卖出形成反向持仓。入场单撤销时没有成交的部分不生成子单。
- **券商原生**（native_brackets=True，broker_paper / live）：下单时三张单一起发给券商（父子单），
  入场单一成交，保护单就已经在券商端生效，即使本机程序崩溃也有保护。两张保护单之间的互相缩减由券商的
  OCA 规则完成；这里只在入场单终结且只部分成交时，把保护单改成实际成交的数量。

实盘相关（阶段 6）：
- **发件箱**：deferred=True 时，下单、撤单、改单请求先进入 outbox，
  由引擎在落盘提交后调用 flush() 发出；回测和回放 deferred=False，立即发出。
- **订单号**：默认“来源-序号”，跳过已有订单号（崩溃恢复后不重复）。
  输入事件与它产生的订单在同一事务里落盘，未处理完的输入重新处理时不会已有订单。
  可选的 id_provider 生成确定性订单号时，已存在的订单不重复创建（幂等）。
- **收盘前平仓**：撤单请求发出后，等到该标的没有任何未终结订单（撤单确认或成交）
  才按实际持仓发平仓单。
- 回报中的异常（迟到成交、无法识别的状态、未知订单的回报）通过 on_system 报告，
  不抛错。
"""

from __future__ import annotations

import copy
import dataclasses
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from trader.core.models import (
    CommissionUpdate,
    Fill,
    Order,
    OrderIntent,
    OrderStatus,
    Reason,
)
from trader.oms.orders import (
    BrokerUpdate,
    CancelRequested,
    LocalUpdate,
    Rejected,
    ReplaceRequested,
    reduce,
    reduce_commission,
)
from trader.oms.portfolio import Portfolio
from trader.oms.risk import Reject, RiskLimits, RiskView, check, is_exit, signature


class ExecutionVenue(Protocol):
    """执行器：返回立即可知的回报（模拟撮合同步返回；IBKR 执行器返回空列表，回报异步到达）。"""

    def submit(self, order: Order, effective: int, now: int) -> list[BrokerUpdate]: ...
    def cancel(self, client_order_id: str, now: int) -> list[BrokerUpdate]: ...
    def modify(
        self, order: Order, new_intent: OrderIntent, effective: int, now: int
    ) -> list[BrokerUpdate]: ...


@dataclass(slots=True)
class SessionState:
    """引擎维护的交易时段状态，供风控使用。"""

    session: str | None = None
    allowed_sessions: frozenset[str] = frozenset({"regular"})
    regular_open: int | None = None
    flatten_at: int | None = None
    flattening: bool = False
    day_start_equity: float = 0.0


@dataclass(slots=True)
class Notice:
    """需要回调策略的事件。"""

    kind: str  # "order" / "fill"
    order: Order
    fill: Fill | None = None


@dataclass
class OrderManager:
    venue: ExecutionVenue
    portfolio: Portfolio
    limits: RiskLimits
    whitelist: frozenset[str]
    decision_delay_ns: int = 0
    session: SessionState = field(default_factory=SessionState)
    orders: dict[str, Order] = field(default_factory=dict)
    owners: dict[str, str] = field(default_factory=dict)
    last_bar_time: dict[str, int] = field(default_factory=dict)
    exec_to_order: dict[str, str] = field(default_factory=dict)
    exec_commission: dict[str, Decimal] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)
    _seq: int = 0
    _recent: dict[str, list[int]] = field(default_factory=dict)
    _signatures: dict[str, list[tuple[int, tuple]]] = field(default_factory=dict)
    on_notice: Callable[[Notice], None] | None = None
    on_system: Callable[[int, str, str], None] | None = None  # (时间, 订单号或标的, 说明)
    deferred: bool = False
    outbox: list[tuple[str, Order, OrderIntent | None, int]] = field(default_factory=list)
    id_provider: Callable[[str], str] | None = None
    _flatten_pending: dict[str, tuple[str, bool]] = field(default_factory=dict)
    # 人工暂停（实盘版）：被暂停的来源只能发减仓单；halt_all_new 对所有来源生效
    halted: set[str] = field(default_factory=set)
    halt_all_new: bool = False
    commission_log: list[CommissionUpdate] = field(default_factory=list)  # 落盘用
    native_brackets: bool = False
    _notes_seen: dict[str, int] = field(default_factory=dict)

    # ---------- 标的归属（DESIGN.md 7.4） ----------

    def claim(self, symbol: str, owner: str) -> None:
        cur = self.owners.get(symbol)
        if cur is not None and cur != owner:
            raise ValueError(f"{symbol} 已属于 {cur}，{owner} 无法占用")
        self.owners[symbol] = owner

    def release(self, symbol: str) -> None:
        if self.portfolio.position(symbol).qty != 0 or self.open_orders(symbol):
            raise ValueError(f"{symbol} 还有持仓或未终结订单，不能释放")
        self.owners.pop(symbol, None)

    # ---------- 查询 ----------

    def open_orders(self, symbol: str | None = None, source: str | None = None) -> list[Order]:
        return [
            o
            for o in self.orders.values()
            if not o.status.is_terminal
            and (symbol is None or o.intent.symbol == symbol)
            and (source is None or o.intent.source == source)
        ]

    def _pending(self, exclude: str | None) -> dict[str, tuple[int, int]]:
        out: dict[str, tuple[int, int]] = {}
        for o in self.open_orders():
            if o.client_order_id == exclude:
                continue
            if o.parent_id is not None and self.orders[o.parent_id].filled_qty == 0:
                continue  # 原生括号单里尚未激活的保护单不占敞口
            b, s = out.get(o.intent.symbol, (0, 0))
            if o.intent.side == "BUY":
                b += o.remaining_qty
            else:
                s += o.remaining_qty
            out[o.intent.symbol] = (b, s)
        return out

    def view(self, now: int, source: str, exclude: str | None = None) -> RiskView:
        p, ss = self.portfolio, self.session
        return RiskView(
            now=now,
            whitelist=self.whitelist,
            owners=self.owners,
            positions={s: pos.qty for s, pos in p.positions.items()},
            pending=self._pending(exclude),
            last_price=p.last_price,
            last_bar_time=self.last_bar_time,
            gross_exposure=p.gross_exposure(),
            daily_pnl=p.equity() - ss.day_start_equity,
            session=ss.session,
            allowed_sessions=ss.allowed_sessions,
            regular_open=ss.regular_open,
            flatten_at=ss.flatten_at,
            flattening=ss.flattening,
            recent_orders=self._recent.get(source, []),
            recent_signatures=self._signatures.get(source, []),
        )

    # ---------- 下单、撤单、改单 ----------

    def _new_id(self, source: str) -> str:
        if self.id_provider is not None:
            return self.id_provider(source)
        # 恢复后序号从 0 重新数：跳过已有的订单号，否则新单会被当成“已存在”而不发出
        while True:
            self._seq += 1
            coid = f"{source}-{self._seq:06d}"
            if coid not in self.orders:
                return coid

    def submit(
        self,
        intent: OrderIntent,
        now: int,
        *,
        exit_order: bool | None = None,
        parent_id: str | None = None,
        role: str = "single",
    ) -> Order:
        coid = self._new_id(intent.source)
        existing = self.orders.get(coid)
        if existing is not None:  # 崩溃恢复后重新处理同一个输入事件：不重复下单
            return existing
        order = Order(intent=intent, client_order_id=coid, created_at=now)
        order.status_times[OrderStatus.NEW] = now
        order.parent_id, order.role = parent_id, role
        if parent_id:
            order.oca_group = parent_id
        if intent.is_bracket and role == "single":
            order.role = "entry"
        self.orders[order.client_order_id] = order
        view = self.view(now, intent.source, exclude=order.client_order_id)
        rej = check(intent, view, self.limits, exit_order=exit_order)
        halted = self.halt_all_new or intent.source in self.halted
        if rej is None and halted:
            exiting = exit_order if exit_order is not None else is_exit(intent, view)
            if not exiting:
                why = "已停止新开仓" if self.halt_all_new else f"{intent.source} 已暂停"
                rej = Reject("halted", why + "，只允许减仓")
        self._recent.setdefault(intent.source, []).append(now)
        self._signatures.setdefault(intent.source, []).append((now, signature(intent)))
        self._trim(intent.source, now)
        if rej is not None:
            self._apply(Rejected(order.client_order_id, now, rej.rule, rej.detail))
            return order
        order.status = OrderStatus.SUBMITTED
        order.status_times[OrderStatus.SUBMITTED] = now
        self._notify(Notice("order", order))
        if self.native_brackets and order.role == "entry":
            self._create_native_children(order, now)
            self._send("bracket", order, None, now)
        else:
            self._send("submit", order, None, now)
        return order

    def _create_native_children(self, parent: Order, now: int) -> None:
        """原生括号单：保护单与入场单同时创建（数量 = 入场数量），随入场单一起发出。"""
        for role, intent in self._child_intents(parent, parent.intent.qty):
            child = Order(
                intent=intent, client_order_id=self._new_id(intent.source), created_at=now
            )
            if child.client_order_id in self.orders:  # 恢复后重新处理同一事件
                continue
            child.parent_id, child.oca_group, child.role = (
                parent.client_order_id,
                parent.client_order_id,
                role,
            )
            child.status_times[OrderStatus.NEW] = now
            child.status = OrderStatus.SUBMITTED
            child.status_times[OrderStatus.SUBMITTED] = now
            self.orders[child.client_order_id] = child
            self._notify(Notice("order", child))

    # ---------- 发件箱 ----------

    def _send(self, action: str, order: Order, intent: OrderIntent | None, now: int) -> None:
        if self.deferred:
            self.outbox.append((action, order, intent, now))
        else:
            self._dispatch(action, order, intent, now)

    def _dispatch(self, action: str, order: Order, intent: OrderIntent | None, now: int) -> None:
        coid = order.client_order_id
        if action == "submit":
            updates = self.venue.submit(order, now + self.decision_delay_ns, now)
        elif action == "bracket":
            updates = self.venue.submit_bracket(  # type: ignore[attr-defined]
                order, self._children(order), now + self.decision_delay_ns, now
            )
        elif action == "cancel":
            updates = self.venue.cancel(coid, now)
        else:
            assert intent is not None
            updates = self.venue.modify(order, intent, now + self.decision_delay_ns, now)
        for u in updates:
            self._apply(u)

    def flush(self) -> int:
        """发出发件箱里的请求（引擎在落盘提交之后调用），返回发出的条数。"""
        n = 0
        while self.outbox:
            action, order, intent, now = self.outbox.pop(0)
            self._dispatch(action, order, intent, now)
            n += 1
        return n

    def _trim(self, source: str, now: int) -> None:
        horizon = now - 60 * 10**9
        self._recent[source] = [t for t in self._recent[source] if t >= horizon]
        self._signatures[source] = [(t, s) for t, s in self._signatures[source] if t >= horizon]

    def cancel(self, client_order_id: str, now: int) -> None:
        order = self.orders.get(client_order_id)
        if order is None or order.status.is_terminal or order.status == OrderStatus.PENDING_CANCEL:
            return
        self._apply(CancelRequested(client_order_id, now))
        self._send("cancel", order, None, now)

    def modify(
        self,
        client_order_id: str,
        now: int,
        *,
        qty: int | None = None,
        limit_price: Decimal | None = None,
        stop_price: Decimal | None = None,
    ) -> Reject | None:
        """改单（例如移动止损）。新的数量不能小于已成交量；改单后重新做风控（不重复计数）。"""
        order = self.orders.get(client_order_id)
        if order is None or order.status.is_terminal:
            return Reject("modify", "订单不存在或已终结")
        changes: dict[str, object] = {}
        if qty is not None:
            if qty <= order.filled_qty:
                return Reject("modify", "新数量不能小于等于已成交量")
            changes["qty"] = qty
        if limit_price is not None:
            changes["limit_price"] = limit_price
        if stop_price is not None:
            changes["stop_price"] = stop_price
        new_intent = dataclasses.replace(order.intent, **changes)  # type: ignore[arg-type]
        exit_hint = True if order.role in ("take_profit", "stop_loss") else None
        rej = check(
            dataclasses.replace(new_intent, qty=new_intent.qty - order.filled_qty),
            self.view(now, order.intent.source, exclude=client_order_id),
            self.limits,
            exit_order=exit_hint,
            count_once=False,
        )
        if rej is not None:
            return rej
        self._apply(ReplaceRequested(client_order_id, now, new_intent))
        self._send("modify", order, new_intent, now)
        return None

    # ---------- 回报处理 ----------

    def handle(self, updates: Sequence[BrokerUpdate | LocalUpdate | CommissionUpdate]) -> None:
        for u in updates:
            self._apply(u)

    def _apply(self, u: BrokerUpdate | LocalUpdate | CommissionUpdate) -> None:
        if isinstance(u, CommissionUpdate):
            base = u.broker_exec_id.split(":")[0]  # “:min” 是同一笔成交的最低收费补足
            coid = self.exec_to_order.get(base)
            if coid is None:
                self._system(0, base, f"收到未知成交的手续费 {u.commission}")
                return
            added = reduce_commission(self.orders[coid], u)
            if added:
                self.commission_log.append(u)
                self.exec_commission[base] = self.exec_commission.get(base, Decimal(0)) + added
                self.portfolio.apply_commission(added)
            return
        order = self.orders.get(u.client_order_id)
        if order is None:
            self._system(u.ts, u.client_order_id, f"收到未知订单的回报：{type(u).__name__}")
            return
        if isinstance(u, Fill) and u.broker_exec_id in self.exec_to_order:
            return  # 重复的成交回报
        before = (order.status, order.filled_qty)
        old_fill = None
        if isinstance(u, Fill) and u.corrects is not None:
            old_fill = next(
                (f for f in order.fills.values() if f.broker_exec_id == u.corrects), None
            )
        reduce(order, u)
        if isinstance(u, Fill):
            self.exec_to_order[u.broker_exec_id] = order.client_order_id
            self.fills.append(u)
            sym, side = order.intent.symbol, order.intent.side
            if old_fill is not None:  # 成交更正：冲回原成交，再按新版本入账
                undo = "SELL" if side == "BUY" else "BUY"
                self.portfolio.apply_fill(sym, undo, old_fill.qty, old_fill.price)  # type: ignore[arg-type]
            self.portfolio.apply_fill(sym, side, u.qty, u.price)
            self._notify(Notice("fill", order, u))
        if (order.status, order.filled_qty) != before:
            self._notify(Notice("order", order))
        self._report_notes(order, u.ts)
        self._after_change(order, u.ts)
        self._check_flatten(u.ts)

    def _report_notes(self, order: Order, ts: int) -> None:
        seen = self._notes_seen.get(order.client_order_id, 0)
        for note in order.notes[seen:]:
            self._system(ts, order.client_order_id, note)
        self._notes_seen[order.client_order_id] = len(order.notes)

    def _system(self, ts: int, ref: str, msg: str) -> None:
        if self.on_system is not None:
            self.on_system(ts, ref, msg)

    def _notify(self, n: Notice) -> None:
        if self.on_notice is not None:
            self.on_notice(Notice(n.kind, copy.copy(n.order), n.fill))

    # ---------- 括号单 ----------

    def _after_change(self, order: Order, now: int) -> None:
        if order.role == "entry":
            self._sync_children(order, now)
        elif order.role in ("take_profit", "stop_loss") and order.parent_id:
            self._sync_children(self.orders[order.parent_id], now)

    def _children(self, parent: Order) -> list[Order]:
        return [o for o in self.orders.values() if o.parent_id == parent.client_order_id]

    def _sync_children(self, parent: Order, now: int) -> None:
        if self.native_brackets:
            self._sync_native_children(parent, now)
            return
        children = self._children(parent)
        exits_filled = sum(c.filled_qty for c in children)
        target = parent.filled_qty - exits_filled  # 还需要保护的数量
        active = [c for c in children if not c.status.is_terminal]
        if target <= 0:
            for c in active:
                self.cancel(c.client_order_id, now)
            return
        if not children:
            self._create_children(parent, target, now)
            return
        for c in active:
            if c.status == OrderStatus.PENDING_CANCEL:
                continue
            want = c.filled_qty + target
            asked = (c.pending_intent or c.intent).qty  # 改单请求已发出时按请求的新数量比较
            if want != asked:
                self.modify(c.client_order_id, now, qty=want)

    def _sync_native_children(self, parent: Order, now: int) -> None:
        """入场单还在工作：保护单由券商管理，不动。入场单终结后：没有成交则撤销保护单
        （券商通常已自动撤销，重复撤单无害）；部分成交则把保护单改成实际需要保护的数量。"""
        if not parent.status.is_terminal or parent.filled_qty >= parent.intent.qty:
            return
        children = self._children(parent)
        active = [c for c in children if not c.status.is_terminal]
        if parent.filled_qty == 0:
            for c in active:
                self.cancel(c.client_order_id, now)
            return
        target = parent.filled_qty - sum(c.filled_qty for c in children)
        for c in active:
            if c.status == OrderStatus.PENDING_CANCEL:
                continue
            want = c.filled_qty + max(target, 0)
            if want <= c.filled_qty:
                self.cancel(c.client_order_id, now)
            elif want != (c.pending_intent or c.intent).qty:
                self.modify(c.client_order_id, now, qty=want)

    def _child_intents(self, parent: Order, qty: int) -> list[tuple[str, OrderIntent]]:
        it = parent.intent
        side = "SELL" if it.side == "BUY" else "BUY"
        base = {
            "source": it.source,
            "symbol": it.symbol,
            "side": side,
            "qty": qty,
            "tif": it.tif if it.tif == "DAY" else "DAY",
            "outside_rth": it.outside_rth,
        }
        ctx = dict(it.reason.context)
        out: list[tuple[str, OrderIntent]] = []
        if it.take_profit is not None:
            out.append((
                "take_profit",
                OrderIntent(
                    **base,  # type: ignore[arg-type]
                    order_type="LMT",
                    limit_price=it.take_profit,
                    reason=Reason("take_profit", f"括号单止盈（入场单 {parent.client_order_id}）",
                                  ctx),
                ),
            ))  # fmt: skip
        if it.stop_loss is not None:
            out.append((
                "stop_loss",
                OrderIntent(
                    **base,  # type: ignore[arg-type]
                    order_type="STP",
                    stop_price=it.stop_loss,
                    reason=Reason("stop_loss", f"括号单止损（入场单 {parent.client_order_id}）",
                                  ctx),
                ),
            ))  # fmt: skip
        return out

    def _create_children(self, parent: Order, qty: int, now: int) -> None:
        for role, intent in self._child_intents(parent, qty):
            self.submit(intent, now, exit_order=True, parent_id=parent.client_order_id, role=role)

    # ---------- 收盘前平仓（DESIGN.md 8.4） ----------

    def flatten(
        self, now: int, symbols: list[str], code: str = "eod_flatten", outside_rth: bool = False
    ) -> None:
        """停止新开仓 → 撤销全部挂单 → 等撤单确认 → 按实际持仓发平仓单。

        常规时段内用市价单；盘后（outside_rth）只能用限价单，按最新价让价 1% 的可成交限价。
        不等撤单确认就平仓，括号单的子单可能同时成交，把持仓打成反向（DESIGN.md 8.4）。
        """
        self.session.flattening = True
        for sym in symbols:
            self._flatten_pending[sym] = (code, outside_rth)
        for o in self.open_orders():
            if o.intent.symbol in symbols:
                self.cancel(o.client_order_id, now)
        self._check_flatten(now)

    def flatten_pending(self) -> list[str]:
        """已请求平仓、还在等撤单确认的标的。"""
        return list(self._flatten_pending)

    def _check_flatten(self, now: int) -> None:
        for sym in list(self._flatten_pending):
            if any(
                o.intent.symbol == sym and o.intent.reason.code != self._flatten_pending[sym][0]
                for o in self.open_orders()
            ):
                continue  # 还有撤单未确认
            code, outside_rth = self._flatten_pending.pop(sym)
            pos = self.portfolio.position(sym)
            if pos.qty == 0:
                continue
            owner = self.owners.get(sym, "manual")
            side = "SELL" if pos.qty > 0 else "BUY"
            limit = None
            if outside_rth:
                last = self.portfolio.last_price.get(sym, float(pos.avg_cost))
                limit = round_price(last * (0.99 if side == "SELL" else 1.01))
            intent = OrderIntent(
                source=owner,
                symbol=sym,
                side=side,
                qty=abs(pos.qty),
                order_type="LMT" if outside_rth else "MKT",
                limit_price=limit,
                outside_rth=outside_rth,
                reason=Reason(code, "收盘前强制平仓"),
            )
            self.submit(intent, now, exit_order=True)


def round_price(x: float | Decimal) -> Decimal:
    """按美股报价规则取整：1 美元及以上到分，以下到 0.0001。"""
    d = x if isinstance(x, Decimal) else Decimal(str(x))
    q = Decimal("0.01") if d >= 1 else Decimal("0.0001")
    return d.quantize(q)
