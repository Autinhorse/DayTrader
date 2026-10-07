"""模拟撮合规则（DESIGN.md 9.2）：手工构造 1 秒 bar，断言成交时间、价格、数量。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from trader.brokers.sim.matcher import SimBroker, SimConfig
from trader.core.models import (
    Bar,
    CommissionUpdate,
    Fill,
    Order,
    OrderIntent,
    OrderType,
    Reason,
    Side,
)
from trader.core.timeutil import NS_PER_SEC
from trader.oms.orders import Accepted, Cancelled, Expired, Rejected

T0 = 1_000_000 * NS_PER_SEC
R = Reason("test")


def bar(i: int, o, h, low, c, v=1000.0, session="regular") -> Bar:
    return Bar("X", "1s", T0 + i * NS_PER_SEC, o, h, low, c, v, session=session)


def order(
    side: Side = "BUY", qty=100, otype: OrderType = "MKT", limit=None, stop=None, **kw
) -> Order:
    it = OrderIntent(
        source="s",
        symbol="X",
        side=side,
        qty=qty,
        order_type=otype,
        reason=R,
        limit_price=None if limit is None else Decimal(str(limit)),
        stop_price=None if stop is None else Decimal(str(stop)),
        **kw,
    )
    return Order(intent=it, client_order_id=f"o{id(it)}")


def broker(**cfg) -> SimBroker:
    base = {"half_spread_bps": {"default": 0.0}, "impact_k": 0.0}
    base.update(cfg)
    return SimBroker(SimConfig(**base))


def run(b: SimBroker, o: Order, bars: list[Bar], effective_i: int = 0) -> list:
    """提交订单（生效于第 effective_i 根 bar 的开始），依次撮合，并把成交写回订单。"""
    out = list(b.submit(o, T0 + effective_i * NS_PER_SEC, T0))
    for x in bars:
        ups = b.on_bar(x)
        for u in ups:
            if isinstance(u, Fill):
                o.filled_qty += u.qty
        out += ups
    return out


def fills(updates) -> list[tuple[int, Decimal, int]]:
    return [((u.ts - T0) // NS_PER_SEC, u.price, u.qty) for u in updates if isinstance(u, Fill)]


def test_effective_time_only_later_intervals():
    """订单只参与起点不早于生效时间的完整区间。"""
    b = broker()
    ups = run(
        b, order(), [bar(0, 10, 10, 10, 10), bar(1, 11, 11, 11, 11), bar(2, 12, 12, 12, 12)], 1
    )
    assert fills(ups) == [(1, Decimal("11.0000"), 100)]


def test_market_order_adverse_costs():
    b = broker(half_spread_bps={"default": 5.0}, impact_k=10.0, impact_min_volume=1000)
    # 前 60 秒成交量：先喂 10 根 bar 作历史
    for i in range(10):
        b.on_bar(bar(i, 100, 100, 100, 100, v=1000))
    buy = order(qty=100)
    ups = run(b, buy, [bar(10, 100, 101, 99, 100)], 10)
    # 冲击 = 10 × 100 / 10000 = 0.1 基点；总成本 5.1 基点
    assert fills(ups) == [(10, Decimal("100.0510"), 100)]
    d = b.details[[u for u in ups if isinstance(u, Fill)][0].broker_exec_id]
    assert d.spread_cost == pytest.approx(100 * 5 / 1e4 * 100)
    assert not d.out_of_range


def test_market_order_out_of_range_flag():
    b = broker(market_max_participation=0.02)
    for i in range(10):
        b.on_bar(bar(i, 100, 100, 100, 100, v=100))  # 60 秒成交量 1000 股
    ups = run(b, order(qty=500), [bar(10, 100, 100, 100, 100)], 10)
    d = b.details[[u for u in ups if isinstance(u, Fill)][0].broker_exec_id]
    assert d.out_of_range


@pytest.mark.parametrize(
    ("bars", "expect"),
    [
        ([(10.0, 10.1, 9.9, 10.0)], (0, Decimal("10.0000"))),  # 开盘价优于限价：按开盘价
        ([(10.2, 10.3, 10.04, 10.1)], (0, Decimal("10.0500"))),  # 最低价穿过限价：按限价
        (
            [(10.2, 10.3, 10.05, 10.1), (10.2, 10.2, 10.0, 10.1)],
            (1, Decimal("10.0500")),
        ),  # 只触及不算
    ],
)
def test_limit_buy(bars, expect):
    b = broker()
    ups = run(b, order(otype="LMT", limit=10.05), [bar(i, *x) for i, x in enumerate(bars)])
    f = fills(ups)
    assert (f[0][0], f[0][1]) == expect


def test_limit_sell_symmetric():
    b = broker()
    ups = run(b, order("SELL", otype="LMT", limit=10.0), [bar(0, 9.9, 10.01, 9.8, 9.9)])
    assert fills(ups) == [(0, Decimal("10.0000"), 100)]


def test_limit_participation_shared_budget_and_partial():
    """每根 bar 可成交量 = 成交量 × 10%，同一根 bar 上的限价单按生效先后共享。"""
    b = broker(limit_participation=0.1)
    o1 = order(qty=80, otype="LMT", limit=11)
    o2 = order(qty=80, otype="LMT", limit=11)
    b.submit(o1, T0, T0)
    b.submit(o2, T0, T0)
    ups = b.on_bar(bar(0, 10, 10, 10, 10, v=1000))  # 预算 100 股
    got = {u.client_order_id: u.qty for u in ups if isinstance(u, Fill)}
    assert got == {o1.client_order_id: 80, o2.client_order_id: 20}
    o1.filled_qty, o2.filled_qty = 80, 20
    ups = b.on_bar(bar(1, 10, 10, 10, 10, v=1000))
    assert [u.qty for u in ups if isinstance(u, Fill)] == [60]  # 余量留到下一根 bar


def test_stop_gap_fills_at_worse_price():
    b = broker()
    ups = run(b, order("SELL", otype="STP", stop=9.5), [bar(0, 9.0, 9.2, 8.9, 9.1)])
    assert fills(ups) == [(0, Decimal("9.0000"), 100)]  # 跳空低开：按更差的开盘价
    b = broker()
    ups = run(b, order("SELL", otype="STP", stop=9.5), [bar(0, 9.8, 9.9, 9.4, 9.6)])
    assert fills(ups) == [(0, Decimal("9.5000"), 100)]


def test_stop_limit_two_phase():
    """止损限价：触发那根 bar 不成交，下一根起按限价撮合。"""
    b = broker()
    o = order("SELL", otype="STP_LMT", stop=9.5, limit=9.4)
    ups = run(b, o, [bar(0, 9.8, 9.8, 9.3, 9.35), bar(1, 9.35, 9.45, 9.3, 9.4)])
    assert fills(ups) == [(1, Decimal("9.4000"), 100)]


def test_ioc_single_chance():
    b = broker()
    o = order(otype="LMT", limit=9.0, tif="IOC")
    ups = run(b, o, [bar(0, 10, 10, 10, 10), bar(1, 8, 8, 8, 8)])
    assert fills(ups) == []
    assert any(isinstance(u, Cancelled) for u in ups)


def test_oca_both_touchable_takes_stop_and_flags_ambiguous():
    b = broker()
    tp = order("SELL", otype="LMT", limit=11)
    sl = order("SELL", otype="STP", stop=9)
    for o, role in ((tp, "take_profit"), (sl, "stop_loss")):
        o.oca_group, o.role = "g", role
        b.submit(o, T0, T0)
    ups = b.on_bar(bar(0, 10, 11.5, 8.5, 10))
    fs = [u for u in ups if isinstance(u, Fill)]
    assert [f.client_order_id for f in fs] == [sl.client_order_id]
    assert b.details[fs[0].broker_exec_id].ambiguous


def test_commission_minimum_charged_once():
    b = broker(limit_participation=0.1)
    o = order(qty=150, otype="LMT", limit=11)
    ups = run(b, o, [bar(0, 10, 10, 10, 10, v=1000), bar(1, 10, 10, 10, 10, v=1000)])
    comm = sum(u.commission for u in ups if isinstance(u, CommissionUpdate))
    assert comm == Decimal("1")  # 150 股 × 0.005 = 0.75，订单结束时补足到 1 美元，只补一次


def test_commission_per_share_above_minimum():
    b = broker()
    ups = run(b, order(qty=1000), [bar(0, 10, 10, 10, 10)])
    assert sum(u.commission for u in ups if isinstance(u, CommissionUpdate)) == Decimal("5")


def test_outside_rth_rules():
    b = broker()
    # 普通订单在盘前 bar 上不撮合，等到常规时段
    ups = run(b, order(), [bar(0, 10, 10, 10, 10, session="pre"), bar(1, 11, 11, 11, 11)])
    assert fills(ups) == [(1, Decimal("11.0000"), 100)]
    # outside_rth 只接受限价单
    rej = broker().submit(order(outside_rth=True), T0, T0)
    assert isinstance(rej[0], Rejected)
    ok = broker()
    ups = run(
        ok,
        order(otype="LMT", limit=10.5, outside_rth=True),
        [bar(0, 10, 10, 10, 10, session="post")],
    )
    assert fills(ups) == [(0, Decimal("10.0000"), 100)]


def test_day_orders_expire():
    b = broker()
    o = order(otype="LMT", limit=1)
    assert isinstance(b.submit(o, T0, T0)[0], Accepted)
    out = b.expire(T0, outside_rth_too=False)
    assert isinstance(out[0], Expired) and b.working_ids() == []
