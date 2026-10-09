"""IBKR 执行器（DESIGN.md 9.3）：把订单管理模块的请求发给 IB，把 IB 的回报转成归约用的更新。

- client_order_id 写进 IB 的 orderRef：重启或重连后按 orderRef 找回订单，不依赖本机缓存的 orderId。
- 括号单用 IB 原生父子单：入场单 transmit=False，止盈止损单指向入场单，最后一张 transmit=True，
  三张一起生效；入场单成交后保护单立即在券商端工作。两张保护单由 IB 自动互相缩减或撤销。
- 请求排队，由 pump(now) 按速率上限发出（IB 每秒消息数有上限）。
- 发出后 send_timeout_s 秒内没有任何回报 → SendTimeout（订单变为 UNKNOWN），随后向 IB 查询挂单和
  当日成交：找到就按实际状态归约，确实没有才记为失败。不会自动重发。
- 回报全部是异步的：submit/cancel/modify 返回空列表，更新由 drain() 取走。
- 成交更正：IB 的成交编号最后一段递增表示更正，新版本带 corrects 指向旧版本。
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from trader.brokers.ibkr.api import BrokerOrderState, ExecReport, IbApi, OrderSpec
from trader.core.models import Bar, CommissionUpdate, Fill, Order, OrderIntent
from trader.core.timeutil import NS_PER_SEC
from trader.oms.orders import (
    BrokerStatus,
    BrokerUpdate,
    Cancelled,
    LocalUpdate,
    QueryResult,
    Rejected,
    SendTimeout,
)

Update = BrokerUpdate | LocalUpdate | CommissionUpdate

# 只是提示、不影响订单状态的 IB 消息
INFO_CODES = frozenset({399, 404, 2109, 2148, 10349, 2161})
# 撤单失败（订单已成交或已不存在）：只提示
CANCEL_FAIL_CODES = frozenset({161, 10147, 10148})


@dataclass
class _Request:
    action: str  # place / cancel
    coid: str
    order_id: int
    spec: OrderSpec | None = None


def _spec(
    order: Order, intent: OrderIntent, account: str, parent_id: int | None, transmit: bool
) -> OrderSpec:
    ot = {"MKT": "MKT", "LMT": "LMT", "STP": "STP", "STP_LMT": "STP LMT", "STPLMT": "STP LMT"}
    return OrderSpec(
        symbol=intent.symbol,
        action=intent.side,
        qty=intent.qty,
        order_type=ot.get(intent.order_type, intent.order_type),
        order_ref=order.client_order_id,
        limit_price=float(intent.limit_price) if intent.limit_price is not None else None,
        stop_price=float(intent.stop_price) if intent.stop_price is not None else None,
        tif=intent.tif,
        outside_rth=intent.outside_rth,
        account=account,
        parent_id=parent_id,
        transmit=transmit,
    )


@dataclass
class IbkrExecutor:
    api: IbApi
    now: Callable[[], int]
    account: str
    send_timeout_s: float = 10.0
    max_msgs_per_s: int = 20
    details: dict[str, Any] = field(default_factory=dict)  # 与模拟撮合接口一致（没有撮合细节）
    messages: list[tuple[int, str, str]] = field(default_factory=list)  # (时间, 订单号, 提示)
    foreign: dict[int, BrokerOrderState] = field(default_factory=dict)  # orderRef 不认识的 IB 订单
    assigned: list[str] = field(default_factory=list)  # 本轮分配了 IB orderId 的订单（需要落盘）
    _orders: dict[str, Order] = field(default_factory=dict)
    _ids: dict[str, int] = field(default_factory=dict)
    _coids: dict[int, str] = field(default_factory=dict)
    _queue: deque[_Request] = field(default_factory=deque)
    _sent_times: deque[int] = field(default_factory=deque)
    _awaiting: dict[str, int] = field(default_factory=dict)  # 发出后还没有任何回报
    _updates: list[Update] = field(default_factory=list)
    _exec_latest: dict[str, str] = field(default_factory=dict)  # 成交编号前缀 → 最新版本
    _timed_out: list[str] = field(default_factory=list)

    def bind(self) -> None:
        self.api.on_order_status(self._on_status)
        self.api.on_execution(self._on_exec)
        self.api.on_commission(self._on_commission)
        self.api.on_error(self._on_error)

    # ---------- 执行器接口（OrderManager 调用） ----------

    def submit(self, order: Order, effective: int, now: int) -> list[BrokerUpdate]:
        oid = self._assign(order)
        spec = _spec(order, order.intent, self.account, None, True)
        self._queue.append(_Request("place", order.client_order_id, oid, spec))
        return []

    def submit_bracket(
        self, parent: Order, children: list[Order], effective: int, now: int
    ) -> list[BrokerUpdate]:
        pid = self._assign(parent)
        spec = _spec(parent, parent.intent, self.account, None, not children)
        reqs = [_Request("place", parent.client_order_id, pid, spec)]
        for i, c in enumerate(children):
            cid = self._assign(c)
            last = i == len(children) - 1
            reqs.append(_Request("place", c.client_order_id, cid,
                                 _spec(c, c.intent, self.account, pid, last)))  # fmt: skip
        self._queue.extend(reqs)
        return []

    def cancel(self, client_order_id: str, now: int) -> list[BrokerUpdate]:
        oid = self._ids.get(client_order_id)
        if oid is None:
            return [Cancelled(client_order_id, now, "未发出的订单直接撤销")]
        order = self._orders.get(client_order_id)
        in_bracket = order is not None and (
            order.parent_id is not None
            or any(o.parent_id == client_order_id for o in self._orders.values())
        )
        unsent = any(r.coid == client_order_id and r.action == "place" for r in self._queue)
        if unsent and client_order_id not in self._awaiting and not in_bracket:
            # 还在本机队列里没有发出：直接移除（括号单三张一起发，不拆开处理）
            self._queue = deque(r for r in self._queue if r.coid != client_order_id)
            return [Cancelled(client_order_id, now, "未发出的订单直接撤销")]
        self._queue.append(_Request("cancel", client_order_id, oid))
        return []

    def modify(
        self, order: Order, new_intent: OrderIntent, effective: int, now: int
    ) -> list[BrokerUpdate]:
        oid = self._ids.get(order.client_order_id)
        if oid is None:
            return [Rejected(order.client_order_id, now, "broker", "订单还没有发给券商，不能改单")]
        parent = self._ids.get(order.parent_id) if order.parent_id else None
        self._queue.append(
            _Request("place", order.client_order_id, oid,
                     _spec(order, new_intent, self.account, parent, True))
        )  # fmt: skip
        return []

    # 引擎在每根 bar、每个收盘时刻调用模拟撮合；券商自己撮合，这里什么都不做
    def on_bar(self, bar: Bar) -> list[BrokerUpdate]:
        return []

    def expire(self, ts: int, outside_rth_too: bool) -> list[BrokerUpdate]:
        return []

    # ---------- 发送与超时 ----------

    def _assign(self, order: Order) -> int:
        coid = order.client_order_id
        self._orders[coid] = order
        if coid in self._ids:
            return self._ids[coid]
        oid = order.broker_order_id or self.api.next_order_id()
        order.broker_order_id = oid
        order.account = self.account
        self._ids[coid], self._coids[oid] = oid, coid
        self.assigned.append(coid)
        return oid

    def pump(self, now: int) -> int:
        """按速率上限发出排队的请求，并检查发送超时。返回本次发出的条数。"""
        while self._sent_times and now - self._sent_times[0] >= NS_PER_SEC:
            self._sent_times.popleft()
        n = 0
        while self._queue and len(self._sent_times) < self.max_msgs_per_s:
            r = self._queue.popleft()
            try:
                if r.action == "place":
                    assert r.spec is not None
                    self.api.place_order(r.order_id, r.spec)
                    self._awaiting.setdefault(r.coid, now)
                else:
                    self.api.cancel_order(r.order_id)
            except Exception as exc:  # noqa: BLE001 - 断线等：结果不明，按超时处理
                self.messages.append((now, r.coid, f"发送失败：{exc}"))
                if r.action == "place":
                    self._updates.append(SendTimeout(r.coid, now))
                    self._timed_out.append(r.coid)
                continue
            self._sent_times.append(now)
            n += 1
        limit = int(self.send_timeout_s * NS_PER_SEC)
        for coid, t in list(self._awaiting.items()):
            if now - t > limit:
                del self._awaiting[coid]
                self._updates.append(SendTimeout(coid, now))
                self._timed_out.append(coid)
        return n

    def take_timed_out(self) -> list[str]:
        out, self._timed_out = self._timed_out, []
        return out

    def drain(self) -> list[Update]:
        out, self._updates = self._updates, []
        return out

    @property
    def queued(self) -> int:
        return len(self._queue)

    # ---------- 回报 ----------

    def _coid_of(self, order_id: int, order_ref: str) -> str | None:
        if order_ref and order_ref in self._orders:
            return order_ref
        return self._coids.get(order_id)

    def _on_status(self, st: BrokerOrderState) -> None:
        coid = self._coid_of(st.order_id, st.order_ref)
        if coid is None:
            self.foreign[st.order_id] = st
            return
        self._awaiting.pop(coid, None)
        order = self._orders.get(coid)
        if order is not None and st.perm_id:
            order.perm_id = st.perm_id
        self._updates.append(BrokerStatus(coid, self.now(), st.status, st.why_held))

    def _on_exec(self, e: ExecReport) -> None:
        coid = self._coid_of(e.order_id, e.order_ref)
        if coid is None:
            self.messages.append(
                (e.ts, "", f"不认识的成交 {e.exec_id} {e.symbol} {e.side} {e.qty}")
            )
            return
        self._awaiting.pop(coid, None)
        base = e.exec_id.rsplit(".", 1)[0]
        prev = self._exec_latest.get(base)
        corrects = prev if prev is not None and prev != e.exec_id else None
        if prev is None or e.exec_id > prev:
            self._exec_latest[base] = e.exec_id
        self._updates.append(
            Fill(coid, e.ts, Decimal(str(e.price)), int(round(e.qty)), e.exec_id, corrects)
        )
        if e.commission is not None and e.commission < 1e9:
            self._updates.append(CommissionUpdate(e.exec_id, Decimal(str(e.commission))))

    def _on_commission(self, exec_id: str, commission: float) -> None:
        if commission >= 1e9:  # IB 用极大值表示“未知”
            return
        self._updates.append(CommissionUpdate(exec_id, Decimal(str(commission))))

    def _on_error(self, req_id: int, code: int, msg: str, symbol: str | None) -> None:
        coid = self._coids.get(req_id)
        if coid is None:
            return  # 不是订单相关的错误（行情、连接）：由行情源处理
        now = self.now()
        if code in INFO_CODES:
            self.messages.append((now, coid, f"IB {code}: {msg}"))
            return
        self._awaiting.pop(coid, None)
        if code in CANCEL_FAIL_CODES and "Cancelled" in msg:
            # 括号单的一张保护单被撤时 IB 会联动撤销另一张，我们随后的撤单就会收到 10148
            self.messages.append((now, coid, f"订单已是撤销状态，无需处理（IB {code}）"))
        elif code in CANCEL_FAIL_CODES:
            self.messages.append((now, coid, f"撤单未成功 IB {code}: {msg}"))
        elif code == 202:
            self._updates.append(Cancelled(coid, now, msg))
        elif code < 2000 or code >= 10000:
            self._updates.append(Rejected(coid, now, "broker", f"IB {code}: {msg}"))
        else:
            self.messages.append((now, coid, f"IB {code}: {msg}"))

    # ---------- 查询与重连 ----------

    def adopt(self, orders: list[Order]) -> None:
        """重启后：把恢复的订单（带 broker_order_id）登记回映射表。"""
        for o in orders:
            self._orders[o.client_order_id] = o
            if o.broker_order_id:
                self._ids[o.client_order_id] = o.broker_order_id
                self._coids[o.broker_order_id] = o.client_order_id

    async def sync(self, query: list[str] | None = None) -> None:
        """拉取 IB 的全部挂单和当日成交，按 orderRef 归约；query 里的订单给出“找到 / 没找到”的结论。

        已知成交按成交编号去重，重复送入无害。
        """
        open_orders = await self.api.open_orders()
        execs = await self.api.executions()
        seen: set[str] = set()
        for st in open_orders:
            coid = self._coid_of(st.order_id, st.order_ref)
            if coid is not None:
                self._ids.setdefault(coid, st.order_id)
                self._coids.setdefault(st.order_id, coid)
                seen.add(coid)
            self._on_status(st)
        for e in sorted(execs, key=lambda x: (x.ts, x.exec_id)):
            coid = self._coid_of(e.order_id, e.order_ref)
            if coid is not None:
                seen.add(coid)
            self._on_exec(e)
        now = self.now()
        for coid in query or []:
            self._updates.append(QueryResult(coid, now, found=coid in seen))
