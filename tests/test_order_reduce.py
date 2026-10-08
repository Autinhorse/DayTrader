"""订单归约的券商边界情况（DESIGN.md 8.1、13）：每个用例是一串回报，断言最终的订单、持仓和成交账本。

重复通知、成交先于确认、撤单途中成交、撤单后补到成交、手续费晚到、发送超时后查询、成交更正、
改单途中成交、改单被拒、无法识别的状态、未知订单的回报。
"""

from __future__ import annotations

from decimal import Decimal

from trader.core.models import CommissionUpdate, Fill, Order, OrderIntent, OrderStatus, Reason
from trader.core.timeutil import NS_PER_MIN, NS_PER_SEC
from trader.oms.manager import OrderManager
from trader.oms.orders import (
    Accepted,
    BrokerStatus,
    BrokerUpdate,
    Cancelled,
    QueryResult,
    Rejected,
    SendTimeout,
)
from trader.oms.portfolio import Portfolio
from trader.oms.risk import RiskLimits

NOW = 10_000 * NS_PER_SEC
D = Decimal


class AsyncVenue:
    """像 IBKR 一样：请求发出后不立即返回回报，回报由测试按顺序喂进来。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def submit(self, order: Order, effective: int, now: int) -> list[BrokerUpdate]:
        self.sent.append(("submit", order.client_order_id))
        return []

    def cancel(self, client_order_id: str, now: int) -> list[BrokerUpdate]:
        self.sent.append(("cancel", client_order_id))
        return []

    def modify(self, order: Order, new_intent, effective: int, now: int) -> list[BrokerUpdate]:  # noqa: ANN001
        self.sent.append(("modify", order.client_order_id))
        return []


def setup(**kw) -> tuple[OrderManager, AsyncVenue, list[tuple[int, str, str]]]:
    venue = AsyncVenue()
    m = OrderManager(venue, Portfolio(D(100_000)), RiskLimits(), frozenset({"X"}), **kw)
    m.claim("X", "s")
    m.portfolio.update_mark("X", 100.0)
    m.last_bar_time["X"] = NOW
    m.session.session = "regular"
    m.session.flatten_at = NOW + 120 * NS_PER_MIN
    system: list[tuple[int, str, str]] = []
    m.on_system = lambda ts, ref, msg: system.append((ts, ref, msg))
    return m, venue, system


def buy(m: OrderManager, qty: int = 100, **kw) -> Order:
    it = OrderIntent(
        source="s", symbol="X", side="BUY", qty=qty, order_type=kw.pop("otype", "LMT"),
        limit_price=kw.pop("limit_price", D("100")), reason=Reason("t"), **kw,
    )  # fmt: skip
    return m.submit(it, NOW)


def fill(coid: str, qty: int, price: str, exec_id: str, corrects: str | None = None) -> Fill:
    return Fill(coid, NOW + 1, D(price), qty, exec_id, corrects)


def test_duplicate_notifications_are_idempotent():
    m, _, _ = setup()
    o = buy(m)
    c = o.client_order_id
    m.handle(
        [Accepted(c, NOW), Accepted(c, NOW), fill(c, 40, "100", "e1"), fill(c, 40, "100", "e1")]
    )
    m.handle([BrokerStatus(c, NOW, "Submitted"), BrokerStatus(c, NOW, "Submitted")])
    assert (o.status, o.filled_qty) == (OrderStatus.PARTIALLY_FILLED, 40)
    assert m.portfolio.position("X").qty == 40
    assert len(m.fills) == 1


def test_fill_before_acknowledgement():
    m, _, _ = setup()
    o = buy(m)
    c = o.client_order_id
    m.handle([fill(c, 100, "99.5", "e1"), Accepted(c, NOW), BrokerStatus(c, NOW, "Submitted")])
    assert o.status == OrderStatus.FILLED and o.avg_fill_price == D("99.5")
    m.handle([BrokerStatus(c, NOW, "Filled")])  # 状态通知不改变成交
    assert m.portfolio.position("X").qty == 100


def test_fill_while_cancel_pending():
    m, venue, _ = setup()
    o = buy(m)
    c = o.client_order_id
    m.handle([Accepted(c, NOW)])
    m.cancel(c, NOW)
    assert o.status == OrderStatus.PENDING_CANCEL and ("cancel", c) in venue.sent
    m.handle([fill(c, 30, "100", "e1")])
    assert o.status == OrderStatus.PENDING_CANCEL and o.filled_qty == 30  # 撤单途中成交照常入账
    m.handle([Cancelled(c, NOW)])
    assert o.status == OrderStatus.CANCELLED
    assert m.portfolio.position("X").qty == 30
    m.cancel(c, NOW)  # 已终结：不再发撤单
    assert venue.sent.count(("cancel", c)) == 1


def test_fill_arriving_after_cancelled_is_booked_and_reported():
    m, _, system = setup()
    o = buy(m)
    c = o.client_order_id
    m.handle([Accepted(c, NOW), Cancelled(c, NOW), fill(c, 20, "100", "e9")])
    assert o.status == OrderStatus.CANCELLED and o.filled_qty == 20
    assert m.portfolio.position("X").qty == 20  # 持仓以成交为准
    assert any("之后又收到成交" in msg for _, _, msg in system)


def test_late_commission():
    m, _, _ = setup()
    o = buy(m)
    c = o.client_order_id
    m.handle([fill(c, 50, "100", "e1"), fill(c, 50, "100", "e2")])
    assert o.commission_pending
    m.handle([CommissionUpdate("e1", D("0.25"))])
    assert o.commission_pending  # 还差 e2
    dup = [CommissionUpdate("e2", D("0.25")), CommissionUpdate("e2", D("0.25"))]
    m.handle(dup)
    assert not o.commission_pending and o.commission == D("0.50")  # 重复的手续费回报不重复记
    assert m.portfolio.commission == D("0.50")


def test_send_timeout_then_query():
    m, _, _ = setup()
    a, b = buy(m), buy(m, qty=50)  # 数量不同，否则第二张会被“重复订单”规则拒绝
    m.handle([SendTimeout(a.client_order_id, NOW), SendTimeout(b.client_order_id, NOW)])
    assert a.status == b.status == OrderStatus.UNKNOWN
    # 未知订单的在途数量仍计入风控敞口（按全部会成交计算）
    assert m._pending(None)["X"] == (150, 0)
    m.handle([QueryResult(a.client_order_id, NOW, found=True)])
    m.handle([QueryResult(b.client_order_id, NOW, found=False)])
    assert a.status == OrderStatus.ACCEPTED
    assert b.status == OrderStatus.REJECTED and b.reject_rule == "not_received"


def test_fill_correction_keeps_both_records():
    m, _, system = setup()
    o = buy(m)
    c = o.client_order_id
    m.handle([fill(c, 100, "100.10", "e1.01")])
    m.handle([fill(c, 100, "100.05", "e1.02", corrects="e1.01")])
    assert o.filled_qty == 100 and o.avg_fill_price == D("100.05")
    assert {f.broker_exec_id for f in m.fills} == {"e1.01", "e1.02"}  # 原记录保留
    pos = m.portfolio.position("X")
    assert pos.qty == 100 and pos.avg_cost == D("100.05")
    assert any("成交更正" in msg for _, _, msg in system)


def test_fill_during_replace_and_replace_rejected():
    m, venue, system = setup()
    o = buy(m, qty=100)
    c = o.client_order_id
    m.handle([Accepted(c, NOW)])
    assert m.modify(c, NOW, limit_price=D("100.5")) is None
    assert o.status == OrderStatus.PENDING_REPLACE and o.intent.limit_price == D("100")
    m.handle([fill(c, 10, "100", "e1")])  # 改单途中按旧价成交
    assert o.status == OrderStatus.PENDING_REPLACE and o.filled_qty == 10
    m.handle([BrokerStatus(c, NOW, "Submitted")])  # 券商确认改单
    assert o.status == OrderStatus.PARTIALLY_FILLED and o.intent.limit_price == D("100.5")
    m.modify(c, NOW, limit_price=D("101"))
    m.handle([Rejected(c, NOW, "broker", "价格不合法")])
    assert o.status == OrderStatus.PARTIALLY_FILLED and o.intent.limit_price == D("100.5")
    assert any("改单被拒" in msg for _, _, msg in system)


def test_unknown_status_and_unknown_order_do_not_raise():
    m, _, system = setup()
    o = buy(m)
    m.handle([BrokerStatus(o.client_order_id, NOW, "WeirdStatus", "原文")])
    m.handle([Accepted("no-such-order", NOW), fill("no-such-order", 1, "1", "zz")])
    m.handle([CommissionUpdate("never-seen", D("1"))])
    msgs = [msg for _, _, msg in system]
    assert any("无法识别的券商状态" in x for x in msgs)
    assert sum("未知订单" in x for x in msgs) == 2
    assert any("未知成交的手续费" in x for x in msgs)
    assert o.status == OrderStatus.SUBMITTED  # 状态不受影响


def test_inactive_status_rejects():
    m, _, _ = setup()
    o = buy(m)
    m.handle([BrokerStatus(o.client_order_id, NOW, "Inactive", "outside RTH")])
    assert o.status == OrderStatus.REJECTED and o.reject_detail == "outside RTH"


def test_deferred_outbox_and_deterministic_ids():
    """落盘之前不发单；同一个输入事件重新处理时订单号相同，不会重复创建。"""
    m, venue, _ = setup(deferred=True)
    seq = iter(range(1, 100))
    m.id_provider = lambda source: f"{source}-ev7-{next(seq)}"
    o = buy(m)
    assert o.client_order_id == "s-ev7-1" and venue.sent == []
    assert m.flush() == 1 and venue.sent == [("submit", "s-ev7-1")]
    # 恢复后重新处理同一事件：编号从头生成，返回已存在的订单，不重复发送
    seq = iter(range(1, 100))
    again = buy(m)
    assert again is o and m.flush() == 0


def test_flatten_waits_for_cancel_confirmation():
    """收盘前平仓：撤单确认之前不发平仓单（否则子单可能同时成交，把持仓打成反向）。"""
    m, venue, _ = setup()
    o = buy(m, qty=50, otype="MKT", limit_price=None)
    c = o.client_order_id
    m.handle([fill(c, 50, "100", "e1")])
    stop = m.submit(
        OrderIntent(
            source="s",
            symbol="X",
            side="SELL",
            qty=50,
            order_type="STP",
            stop_price=D("95"),
            reason=Reason("stop"),
        ),  # fmt: skip
        NOW,
    )
    m.flatten(NOW, ["X"])
    closes = [x for x in m.orders.values() if x.intent.reason.code == "eod_flatten"]
    assert closes == [] and m.flatten_pending() == ["X"]  # 还在等撤单确认
    m.handle([fill(stop.client_order_id, 50, "95", "e2")])  # 撤单途中止损成交了
    m.handle([Cancelled(stop.client_order_id, NOW)])
    assert m.portfolio.position("X").qty == 0
    closes = [x for x in m.orders.values() if x.intent.reason.code == "eod_flatten"]
    assert closes == []  # 持仓已经是 0：不发平仓单，不会打成反向
    assert m.flatten_pending() == []
