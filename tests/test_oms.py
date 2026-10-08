"""订单归约、持仓、风控每条规则（通过与拒绝）、括号单不超量（DESIGN.md 8.1–8.3、13）。"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from trader.core.models import Fill, Order, OrderIntent, OrderStatus, OrderType, Reason, Side
from trader.core.timeutil import NS_PER_MIN, NS_PER_SEC
from trader.oms.manager import OrderManager
from trader.oms.orders import Accepted, BrokerUpdate, Cancelled, Replaced, reduce
from trader.oms.portfolio import Portfolio
from trader.oms.risk import RiskLimits, RiskView, check

R = Reason("t")
NOW = 10_000 * NS_PER_SEC


def intent(
    side: Side = "BUY", qty=10, otype: OrderType = "MKT", source="s", symbol="X", **kw
) -> OrderIntent:
    return OrderIntent(
        source=source, symbol=symbol, side=side, qty=qty, order_type=otype, reason=R, **kw
    )


# ---------- 归约 ----------


def test_reduce_idempotent_fills_and_fill_before_accept():
    o = Order(intent=intent(qty=10), client_order_id="a")
    f = Fill("a", 1, Decimal("10"), 4, "e1")
    reduce(o, f)  # 没经过 ACCEPTED 直接成交
    reduce(o, f)  # 重复回报
    assert (o.status, o.filled_qty) == (OrderStatus.PARTIALLY_FILLED, 4)
    reduce(o, Fill("a", 2, Decimal("11"), 6, "e2"))
    assert o.status == OrderStatus.FILLED and o.avg_fill_price == Decimal("10.6")
    reduce(o, Accepted("a", 3))  # 终态后的状态通知不改变结果
    assert o.status == OrderStatus.FILLED


# ---------- 持仓 ----------


def test_portfolio_long_short_and_flip():
    p = Portfolio(Decimal(1000))
    p.apply_fill("X", "BUY", 10, Decimal("10"))
    p.apply_fill("X", "BUY", 10, Decimal("12"))
    assert p.position("X").avg_cost == Decimal("11")
    r = p.apply_fill("X", "SELL", 30, Decimal("13"))  # 平 20 股多头，开 10 股空头
    assert r == Decimal("40")
    pos = p.position("X")
    assert (pos.qty, pos.avg_cost) == (-10, Decimal("13"))
    assert p.apply_fill("X", "BUY", 10, Decimal("12")) == Decimal("10")  # 空头盈利
    assert p.position("X").qty == 0
    assert p.cash == Decimal(1000) + Decimal("50")


# ---------- 风控 ----------


def view(**kw) -> RiskView:
    base = dict(
        now=NOW,
        whitelist=frozenset({"X"}),
        owners={"X": "s"},
        positions={},
        pending={},
        last_price={"X": 100.0},
        last_bar_time={"X": NOW},
        gross_exposure=0.0,
        daily_pnl=0.0,
        session="regular",
        allowed_sessions=frozenset({"regular"}),
        regular_open=NOW - 60 * NS_PER_MIN,
        flatten_at=NOW + 120 * NS_PER_MIN,
        flattening=False,
    )
    base.update(kw)
    return RiskView(**base)  # type: ignore[arg-type]


LIM = RiskLimits()


@pytest.mark.parametrize(
    ("rule", "it", "v", "lim"),
    [
        ("whitelist", intent(symbol="Y"), {}, {}),
        ("ownership", intent(source="other"), {}, {}),
        ("connection", intent(), {"connected": False}, {}),
        ("time_window", intent(), {"flattening": True}, {}),
        ("time_window", intent(), {"session": "pre"}, {}),
        ("time_window", intent(), {"regular_open": NOW - NS_PER_MIN}, {"no_open_first_min": 5}),
        ("time_window", intent(), {"flatten_at": NOW + 5 * NS_PER_MIN}, {}),
        ("data_freshness", intent(), {"last_bar_time": {"X": NOW - 600 * NS_PER_SEC}}, {}),
        ("order_qty", intent(qty=100), {}, {"max_order_qty": 50}),
        ("order_usd", intent(qty=300), {}, {}),
        ("price_protection", intent(otype="LMT", limit_price=Decimal("110")), {}, {}),
        ("short", intent(side="SELL"), {}, {"allow_short": False}),
        ("position_usd", intent(qty=150), {"positions": {"X": 100}}, {}),
        ("position_usd", intent(qty=150), {"pending": {"X": (100, 0)}}, {}),  # 在途订单计入
        ("gross_exposure", intent(qty=150), {"gross_exposure": 90_000.0}, {}),
        ("daily_loss", intent(), {"daily_pnl": -2500.0}, {}),
        ("order_rate", intent(), {"recent_orders": [NOW - NS_PER_SEC] * 10}, {}),
        (
            "duplicate",
            intent(),
            {"recent_signatures": [(NOW - NS_PER_SEC, ("s", "X", "BUY", 10, "MKT", None, None))]},
            {},
        ),
    ],
)
def test_each_rule_rejects(rule, it, v, lim):
    rej = check(it, view(**v), RiskLimits(**lim))
    assert rej is not None and rej.rule == rule, rej


def test_normal_order_passes_every_rule():
    assert check(intent(qty=100), view(), LIM) is None


def test_exits_bypass_opening_limits():
    """减仓单不受开仓类限制：亏损熔断、平仓时段、频率、行情新鲜度都拦不住。"""
    v = view(
        positions={"X": 500},
        daily_pnl=-10_000.0,
        flattening=True,
        last_bar_time={},
        recent_orders=[NOW] * 99,
    )
    assert check(intent(side="SELL", qty=500), v, LIM) is None
    rej = check(intent(side="SELL", qty=600), v, LIM)  # 超过可平持仓就不是减仓单
    assert rej is not None


def test_recheck_does_not_count_twice():
    v = view(recent_orders=[NOW] * 10)
    assert check(intent(), v, LIM, count_once=False) is None


# ---------- 订单管理：括号单 ----------


class FakeVenue:
    def __init__(self) -> None:
        self.submitted: list[Order] = []
        self.cancelled: list[str] = []

    def submit(self, order: Order, effective: int, now: int) -> list[BrokerUpdate]:
        self.submitted.append(order)
        return [Accepted(order.client_order_id, now)]

    def cancel(self, client_order_id: str, now: int) -> list[BrokerUpdate]:
        self.cancelled.append(client_order_id)
        return [Cancelled(client_order_id, now)]

    def modify(self, order: Order, new_intent, effective: int, now: int) -> list[BrokerUpdate]:  # noqa: ANN001
        return [Replaced(order.client_order_id, now)]


def oms() -> tuple[OrderManager, FakeVenue]:
    venue = FakeVenue()
    m = OrderManager(venue, Portfolio(Decimal(100_000)), RiskLimits(), frozenset({"X"}))
    m.claim("X", "s")
    m.portfolio.update_mark("X", 100.0)
    m.last_bar_time["X"] = NOW
    m.session.session = "regular"
    m.session.flatten_at = NOW + 120 * NS_PER_MIN
    return m, venue


def fill(m: OrderManager, coid: str, qty: int, price: str, n: list[int] = [0]) -> None:  # noqa: B006
    n[0] += 1
    m.handle([Fill(coid, NOW, Decimal(price), qty, f"e{n[0]}")])


def test_bracket_children_follow_parent_fills_without_oversell():
    m, venue = oms()
    parent = m.submit(intent(qty=100, take_profit=Decimal("110"), stop_loss=Decimal("95")), NOW)
    assert parent.role == "entry" and len(venue.submitted) == 1
    fill(m, parent.client_order_id, 40, "100")  # 部分成交：子单数量 = 40
    kids = {o.role: o for o in m.orders.values() if o.parent_id == parent.client_order_id}
    assert {r: o.intent.qty for r, o in kids.items()} == {"take_profit": 40, "stop_loss": 40}
    fill(m, parent.client_order_id, 60, "100")  # 全部成交：子单改为 100
    assert {r: o.intent.qty for r, o in kids.items()} == {"take_profit": 100, "stop_loss": 100}
    fill(m, kids["stop_loss"].client_order_id, 30, "95")  # 止损部分成交：止盈缩减为 70 未成交
    tp = kids["take_profit"]
    assert tp.intent.qty - tp.filled_qty == 70
    fill(m, kids["stop_loss"].client_order_id, 70, "95")  # 止损成交完：止盈撤销
    assert tp.status == OrderStatus.CANCELLED
    assert m.portfolio.position("X").qty == 0  # 没有反向持仓


def test_flatten_cancels_then_closes():
    m, venue = oms()
    parent = m.submit(intent(qty=50, stop_loss=Decimal("95")), NOW)
    fill(m, parent.client_order_id, 50, "100")
    m.flatten(NOW, ["X"])
    sl = [o for o in m.orders.values() if o.role == "stop_loss"][0]
    assert sl.status == OrderStatus.CANCELLED
    close = m.orders[venue.submitted[-1].client_order_id]
    assert (close.intent.side, close.intent.qty, close.intent.reason.code) == (
        "SELL",
        50,
        "eod_flatten",
    )
    assert m.session.flattening
    # 平仓后新开仓被拒绝
    assert m.submit(intent(qty=10), NOW).reject_rule == "time_window"


def test_own_order_not_double_counted():
    """回归：风控计算在途敞口时不把正在检查的这张订单算两次。"""
    m, _ = oms()
    o = m.submit(intent(qty=150), NOW)  # 15,000 美元，上限 20,000
    assert o.reject_rule is None


def test_ownership_claim_and_release():
    m, _ = oms()
    with pytest.raises(ValueError):
        m.claim("X", "other")
    o = m.submit(intent(qty=10), NOW)
    fill(m, o.client_order_id, 10, "100")
    with pytest.raises(ValueError):
        m.release("X")  # 还有持仓
    m.submit(intent(side="SELL", qty=10), NOW)
    last = list(m.orders.values())[-1]
    fill(m, last.client_order_id, 10, "100")
    m.release("X")
    assert "X" not in m.owners


def test_modify_rechecks_without_counting():
    m, _ = oms()
    o = m.submit(intent(qty=10, otype="LMT", limit_price=Decimal("99")), NOW)
    assert m.modify(o.client_order_id, NOW, limit_price=Decimal("99.5")) is None
    assert o.intent.limit_price == Decimal("99.5")
    rej = m.modify(o.client_order_id, NOW, qty=500)
    assert rej is not None and o.intent.qty == 10
    assert dataclasses.replace(o.intent, qty=11).qty == 11
