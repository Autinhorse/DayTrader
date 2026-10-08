"""订单归约（DESIGN.md 8.1）：订单状态由 reduce(order, update) 从回报流算出。

规则：
- **幂等**：同一回报重复到达结果不变；成交按 broker_exec_id 去重。
- **成交以成交回报为准**：可以没经过确认就成交；状态通知只作辅助，
  不会因为“Filled”通知就把订单记为成交。
- **撤单 / 改单途中成交**：PENDING_CANCEL / PENDING_REPLACE 期间的成交照常入账。
- **终态后才到的成交**（发生在撤单之前、送达在撤单确认之后）：照常入账，并记一条异常备注。
- **手续费晚到**：有成交而手续费未到齐时 commission_pending 为真，净盈亏视为未结算。
- **发送结果不明**：SendTimeout → UNKNOWN；向券商查询后，确认没收到才记为失败
  （QueryResult(found=False)）。
- **成交更正**：保留原记录，以新版本重算成交量和均价。
- 无法识别的状态或不合逻辑的回报不抛错：记录原文到 notes，由调用方发 SystemEvent。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from trader.core.models import CommissionUpdate, Fill, Order, OrderIntent, OrderStatus

# ---------- 回报类型 ----------


@dataclass(frozen=True, slots=True)
class Accepted:
    client_order_id: str
    ts: int


@dataclass(frozen=True, slots=True)
class Cancelled:
    client_order_id: str
    ts: int
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Rejected:
    client_order_id: str
    ts: int
    rule: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Expired:
    client_order_id: str
    ts: int


@dataclass(frozen=True, slots=True)
class Replaced:
    """改单被执行器确认。"""

    client_order_id: str
    ts: int


@dataclass(frozen=True, slots=True)
class BrokerStatus:
    """券商的原始状态通知（例如 IB 的 orderStatus）。只作辅助，映射见 IB_STATUS。"""

    client_order_id: str
    ts: int
    raw: str
    detail: str = ""


# 本地事件：订单管理模块在发出请求时自己产生


@dataclass(frozen=True, slots=True)
class CancelRequested:
    client_order_id: str
    ts: int


@dataclass(frozen=True, slots=True)
class ReplaceRequested:
    client_order_id: str
    ts: int
    new_intent: OrderIntent


@dataclass(frozen=True, slots=True)
class SendTimeout:
    """发送请求超时或发送时断线：不知道券商是否收到。"""

    client_order_id: str
    ts: int


@dataclass(frozen=True, slots=True)
class QueryResult:
    """向券商查询挂单和当日成交后的结论：found=False 表示券商确实没有这张订单。"""

    client_order_id: str
    ts: int
    found: bool


BrokerUpdate = Accepted | Cancelled | Rejected | Expired | Replaced | BrokerStatus | Fill
LocalUpdate = CancelRequested | ReplaceRequested | SendTimeout | QueryResult
Update = BrokerUpdate | LocalUpdate

# IB 的 orderStatus → 我们的状态（None 表示不改变状态）
IB_STATUS: dict[str, OrderStatus | None] = {
    "ApiPending": None,
    "PendingSubmit": None,
    "PreSubmitted": OrderStatus.ACCEPTED,  # 已被券商接受、等待触发（例如止损单、盘外单）
    "Submitted": OrderStatus.ACCEPTED,
    "PendingCancel": OrderStatus.PENDING_CANCEL,
    "ApiCancelled": OrderStatus.CANCELLED,
    "Cancelled": OrderStatus.CANCELLED,
    "Filled": None,  # 以成交回报为准
    "Inactive": OrderStatus.REJECTED,  # 券商未接受或暂不能执行
}


def _enter(order: Order, status: OrderStatus, ts: int) -> None:
    order.status = status
    order.status_times.setdefault(status, ts)


def _recompute_fills(order: Order) -> None:
    total = sum(f.qty for f in order.fills.values())
    value = sum((f.price * f.qty for f in order.fills.values()), Decimal(0))
    order.filled_qty = total
    order.avg_fill_price = (value / total) if total else None


def _base_exec(order: Order, exec_id: str) -> str | None:
    """被更正的成交最初的 exec_id（更正可以连续发生）。"""
    for base, f in order.fills.items():
        if f.broker_exec_id == exec_id:
            return base
    return None


def _apply_fill(order: Order, f: Fill) -> None:
    if f.broker_exec_id in order.exec_ids:
        return  # 重复回报
    order.exec_ids.add(f.broker_exec_id)
    if f.corrects is not None:
        base = _base_exec(order, f.corrects)
        if base is None:
            order.notes.append(f"更正的原成交 {f.corrects} 不存在，按新成交入账")
            order.fills[f.broker_exec_id] = f
        else:
            order.notes.append(f"成交更正：{f.corrects} → {f.broker_exec_id}")
            order.fills[base] = f
    else:
        order.fills[f.broker_exec_id] = f
    _recompute_fills(order)
    order.commission_pending = bool(set(order.exec_ids) - order.commission_execs)
    if order.filled_qty > order.intent.qty:
        order.notes.append(f"成交 {order.filled_qty} 超过订单数量 {order.intent.qty}")
    if order.status.is_terminal and order.status != OrderStatus.FILLED:
        order.notes.append(f"订单已是 {order.status.value} 之后又收到成交 {f.broker_exec_id}")
        return
    if order.filled_qty >= order.intent.qty:
        _enter(order, OrderStatus.FILLED, f.ts)
    elif order.filled_qty == 0:
        return  # 更正成 0 的极端情况：状态不动
    elif order.status not in (OrderStatus.PENDING_CANCEL, OrderStatus.PENDING_REPLACE):
        _enter(order, OrderStatus.PARTIALLY_FILLED, f.ts)


def reduce(order: Order, update: Update) -> Order:
    """把一条回报或本地事件并入订单（就地修改并返回）。"""
    if isinstance(update, Fill):
        _apply_fill(order, update)
        return order
    if isinstance(update, BrokerStatus):
        order.broker_status = update.raw
        if update.raw not in IB_STATUS:
            order.notes.append(f"无法识别的券商状态：{update.raw} {update.detail}".strip())
            return order
        mapped = IB_STATUS[update.raw]
        if mapped is None or order.status.is_terminal:
            return order
        if mapped == OrderStatus.ACCEPTED and order.status in (
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.PENDING_CANCEL,
        ):
            return order  # 已有成交或正在撤单：Submitted 通知不改变状态
        if mapped == OrderStatus.ACCEPTED and order.status == OrderStatus.PENDING_REPLACE:
            return _finish_replace(order, update.ts)
        if mapped == OrderStatus.REJECTED:
            order.reject_rule, order.reject_detail = "broker", update.detail or update.raw
        _enter(order, mapped, update.ts)
        return order
    if order.status.is_terminal:
        return order
    if isinstance(update, Accepted):
        if order.status in (OrderStatus.NEW, OrderStatus.SUBMITTED, OrderStatus.UNKNOWN):
            _enter(order, OrderStatus.ACCEPTED, update.ts)
    elif isinstance(update, Cancelled):
        _enter(order, OrderStatus.CANCELLED, update.ts)
    elif isinstance(update, Rejected):
        if order.status == OrderStatus.PENDING_REPLACE:  # 改单被拒：恢复原状态
            order.pending_intent = None
            order.notes.append(f"改单被拒：{update.detail}")
            _enter(order, _working_status(order), update.ts)
            return order
        order.reject_rule, order.reject_detail = update.rule, update.detail
        _enter(order, OrderStatus.REJECTED, update.ts)
    elif isinstance(update, Expired):
        _enter(order, OrderStatus.EXPIRED, update.ts)
    elif isinstance(update, Replaced):
        _finish_replace(order, update.ts)
    elif isinstance(update, CancelRequested):
        _enter(order, OrderStatus.PENDING_CANCEL, update.ts)
    elif isinstance(update, ReplaceRequested):
        order.pending_intent = update.new_intent
        _enter(order, OrderStatus.PENDING_REPLACE, update.ts)
    elif isinstance(update, SendTimeout):
        if order.status in (OrderStatus.NEW, OrderStatus.SUBMITTED):
            _enter(order, OrderStatus.UNKNOWN, update.ts)
    elif isinstance(update, QueryResult) and order.status == OrderStatus.UNKNOWN:
        if update.found:
            _enter(order, _working_status(order), update.ts)
        elif order.filled_qty == 0:
            order.reject_rule, order.reject_detail = (
                "not_received",
                "查询确认券商没有收到这张订单",
            )
            _enter(order, OrderStatus.REJECTED, update.ts)
    return order


def _working_status(order: Order) -> OrderStatus:
    return OrderStatus.PARTIALLY_FILLED if order.filled_qty > 0 else OrderStatus.ACCEPTED


def _finish_replace(order: Order, ts: int) -> Order:
    if order.pending_intent is not None:
        order.intent = order.pending_intent
        order.pending_intent = None
    if order.status in (
        OrderStatus.PENDING_REPLACE,
        OrderStatus.ACCEPTED,
        OrderStatus.PARTIALLY_FILLED,
    ):
        status = (
            OrderStatus.FILLED if order.filled_qty >= order.intent.qty else _working_status(order)
        )
        _enter(order, status, ts)
    return order


def reduce_commission(order: Order, update: CommissionUpdate) -> Decimal:
    """手续费回报（可能晚于成交、可能重复）。返回这次新增的金额。"""
    if update.broker_exec_id in order.commission_execs:
        return Decimal(0)
    order.commission_execs.add(update.broker_exec_id)
    order.commission += update.commission
    order.commission_pending = bool(set(order.exec_ids) - order.commission_execs)
    return update.commission
