"""订单归约（DESIGN.md 8.1）：订单状态由 reduce(order, update) 从执行器回报算出。

阶段 3 只实现常规路径（模拟撮合会产生的回报）：确认挂单、成交（可部分）、撤销、拒绝、过期。
券商的各种边界情况（重复通知、成交先于确认、撤单途中成交、发送结果不明等）在阶段 6 加入，
接口已能容纳：成交按 broker_exec_id 去重，可以不经过 ACCEPTED 直接成交。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from trader.core.models import Fill, Order, OrderStatus


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


BrokerUpdate = Accepted | Cancelled | Rejected | Expired | Fill


def _enter(order: Order, status: OrderStatus, ts: int) -> None:
    order.status = status
    order.status_times.setdefault(status, ts)


def reduce(order: Order, update: BrokerUpdate) -> Order:
    """把一条回报并入订单（就地修改并返回）。重复的回报不改变结果。"""
    if isinstance(update, Fill):
        if update.broker_exec_id in order.exec_ids:
            return order
        order.exec_ids.add(update.broker_exec_id)
        total = order.filled_qty + update.qty
        prev_value = (order.avg_fill_price or Decimal(0)) * order.filled_qty
        order.avg_fill_price = (prev_value + update.price * update.qty) / total
        order.filled_qty = total
        if order.status.is_terminal:  # 终态后才送达的成交照常入账（阶段 6 另发 SystemEvent）
            return order
        if order.filled_qty >= order.intent.qty:
            _enter(order, OrderStatus.FILLED, update.ts)
        else:
            _enter(order, OrderStatus.PARTIALLY_FILLED, update.ts)
        return order
    if order.status.is_terminal:
        return order
    if isinstance(update, Accepted):
        if order.status in (OrderStatus.NEW, OrderStatus.SUBMITTED):
            _enter(order, OrderStatus.ACCEPTED, update.ts)
    elif isinstance(update, Cancelled):
        _enter(order, OrderStatus.CANCELLED, update.ts)
    elif isinstance(update, Rejected):
        order.reject_rule, order.reject_detail = update.rule, update.detail
        _enter(order, OrderStatus.REJECTED, update.ts)
    elif isinstance(update, Expired):
        _enter(order, OrderStatus.EXPIRED, update.ts)
    return order
