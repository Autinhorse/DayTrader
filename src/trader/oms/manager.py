"""订单管理（DESIGN.md 8.2）：生成订单号、风控、交给执行器、按回报归约订单并更新持仓。

括号单：入场单成交后由这里生成止盈（限价）和止损（止损单）两张子单，同属一个 OCA 组；
子单数量始终等于"入场单累计成交量 − 已成交的退出量"，一张退出单成交后另一张随之缩减或撤销，
不会超量卖出形成反向持仓。入场单撤销时没有成交的部分不生成子单。

阶段 3 只实现常规路径；实盘的落盘顺序和恢复在阶段 6。
"""

from __future__ import annotations

import copy
import dataclasses
from collections.abc import Callable
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
from trader.oms.orders import BrokerUpdate, Rejected, reduce
from trader.oms.portfolio import Portfolio
from trader.oms.risk import Reject, RiskLimits, RiskView, check, signature


class ExecutionVenue(Protocol):
    def submit(self, order: Order, effective: int, now: int) -> list[BrokerUpdate]: ...
    def cancel(self, client_order_id: str, now: int) -> list[BrokerUpdate]: ...
    def modify(self, order: Order, effective: int) -> None: ...


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
        self._seq += 1
        return f"{source}-{self._seq:06d}"

    def submit(
        self,
        intent: OrderIntent,
        now: int,
        *,
        exit_order: bool | None = None,
        parent_id: str | None = None,
        role: str = "single",
    ) -> Order:
        order = Order(intent=intent, client_order_id=self._new_id(intent.source), created_at=now)
        order.status_times[OrderStatus.NEW] = now
        order.parent_id, order.role = parent_id, role
        if parent_id:
            order.oca_group = parent_id
        if intent.is_bracket and role == "single":
            order.role = "entry"
        self.orders[order.client_order_id] = order
        view = self.view(now, intent.source, exclude=order.client_order_id)
        rej = check(intent, view, self.limits, exit_order=exit_order)
        self._recent.setdefault(intent.source, []).append(now)
        self._signatures.setdefault(intent.source, []).append((now, signature(intent)))
        self._trim(intent.source, now)
        if rej is not None:
            self._apply(Rejected(order.client_order_id, now, rej.rule, rej.detail))
            return order
        order.status = OrderStatus.SUBMITTED
        order.status_times[OrderStatus.SUBMITTED] = now
        self._notify(Notice("order", order))
        for u in self.venue.submit(order, now + self.decision_delay_ns, now):
            self._apply(u)
        return order

    def _trim(self, source: str, now: int) -> None:
        horizon = now - 60 * 10**9
        self._recent[source] = [t for t in self._recent[source] if t >= horizon]
        self._signatures[source] = [(t, s) for t, s in self._signatures[source] if t >= horizon]

    def cancel(self, client_order_id: str, now: int) -> None:
        order = self.orders.get(client_order_id)
        if order is None or order.status.is_terminal:
            return
        for u in self.venue.cancel(client_order_id, now):
            self._apply(u)

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
        order.intent = new_intent
        self.venue.modify(order, now + self.decision_delay_ns)
        self._notify(Notice("order", order))
        return None

    # ---------- 回报处理 ----------

    def handle(self, updates: list[BrokerUpdate]) -> None:
        for u in updates:
            self._apply(u)

    def _apply(self, u: BrokerUpdate | CommissionUpdate) -> None:
        if isinstance(u, CommissionUpdate):
            coid = self.exec_to_order.get(u.broker_exec_id)
            if coid is not None:
                self.orders[coid].commission += u.commission
                self.exec_commission[u.broker_exec_id] = (
                    self.exec_commission.get(u.broker_exec_id, Decimal(0)) + u.commission
                )
                self.portfolio.apply_commission(u.commission)
            return
        order = self.orders.get(u.client_order_id)
        if order is None:
            return
        if isinstance(u, Fill) and u.broker_exec_id in self.exec_to_order:
            return  # 重复的成交回报
        before = (order.status, order.filled_qty)
        reduce(order, u)
        if isinstance(u, Fill):
            self.exec_to_order[u.broker_exec_id] = order.client_order_id
            self.fills.append(u)
            self.portfolio.apply_fill(order.intent.symbol, order.intent.side, u.qty, u.price)
            self._notify(Notice("fill", order, u))
        if (order.status, order.filled_qty) != before:
            self._notify(Notice("order", order))
        self._after_change(order, u.ts)

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
            want = c.filled_qty + target
            if want != c.intent.qty:
                self.modify(c.client_order_id, now, qty=want)

    def _create_children(self, parent: Order, qty: int, now: int) -> None:
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
        if it.take_profit is not None:
            tp = OrderIntent(
                **base,  # type: ignore[arg-type]
                order_type="LMT",
                limit_price=it.take_profit,
                reason=Reason("take_profit", f"括号单止盈（入场单 {parent.client_order_id}）", ctx),
            )
            self.submit(
                tp, now, exit_order=True, parent_id=parent.client_order_id, role="take_profit"
            )
        if it.stop_loss is not None:
            sl = OrderIntent(
                **base,  # type: ignore[arg-type]
                order_type="STP",
                stop_price=it.stop_loss,
                reason=Reason("stop_loss", f"括号单止损（入场单 {parent.client_order_id}）", ctx),
            )
            self.submit(
                sl, now, exit_order=True, parent_id=parent.client_order_id, role="stop_loss"
            )

    # ---------- 收盘前平仓（DESIGN.md 8.4） ----------

    def flatten(
        self, now: int, symbols: list[str], code: str = "eod_flatten", outside_rth: bool = False
    ) -> None:
        """停止新开仓 → 撤销全部挂单 → 按撤单确认后的实际持仓发平仓单。

        常规时段内用市价单；盘后（outside_rth）只能用限价单，按最新价让价 1% 的可成交限价。
        """
        self.session.flattening = True
        for o in self.open_orders():
            if o.intent.symbol in symbols:
                self.cancel(o.client_order_id, now)
        for sym in symbols:
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
