"""broker_paper 端到端（假的 IB 客户端模拟券商）：下单映射、原生括号单、各种券商回报、
发送超时后查询、成交更正、限速、崩溃恢复、对账。永远不连接真实的 IBKR。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest
from tests.fake_ib import FakeIb

from conftest import make_dataset
from test_live_runner import DAY, PREV, T0, UNIVERSE, Clock, config, fake_ib
from trader.brokers.ibkr.api import BrokerPosition
from trader.core.events import SystemKind
from trader.core.models import OrderIntent, OrderStatus, Reason
from trader.core.timeutil import NS_PER_SEC
from trader.data.bar_builder import TradeTick
from trader.engine.engine import MANUAL
from trader.live.runner import LiveRunner

S = NS_PER_SEC
D = Decimal


@pytest.fixture
def dataset(tmp_path: Path):
    return make_dataset(tmp_path, [PREV], symbols=("AAA", "BBB"))


def make(tmp_path: Path, clock: Clock | None = None, ib: FakeIb | None = None, **kw):  # noqa: ANN201
    clock = clock or Clock(T0)
    ib = ib or fake_ib(clock)
    cfg = config(profile="broker_paper", strategy=None, **kw)
    r = LiveRunner(cfg, ib, UNIVERSE, tmp_path, tmp_path / "runs" / "broker", clock)
    r.feed_timing = (0, 0)
    r.clock_sample_gap_s = 0
    return r, ib, clock


def run(coro_fn: Callable) -> None:  # noqa: ANN001
    asyncio.run(coro_fn())


async def tick(r: LiveRunner, ib: FakeIb, clock: Clock, n: int = 1, price: float = 100.0) -> None:
    """推进 n 秒：每秒一笔 AAA 成交（保持行情新鲜），每 100 毫秒一步。"""
    for _ in range(n * 10):
        clock.t += 100 * 10**6
        if clock.t % S < 100 * 10**6 and "AAA" in ib.tick_subs:
            ib.trade(TradeTick("AAA", clock.t // S * S - S, price, 100, clock.t))
        r.step(clock.t)
        await asyncio.sleep(0)


def manual(r: LiveRunner, qty: int, side: str = "BUY", **kw) -> str:
    assert r.engine is not None
    otype = kw.pop("order_type", "MKT")
    it = OrderIntent(source=MANUAL, symbol="AAA", side=side, qty=qty,  # type: ignore[arg-type]
                     order_type=otype, reason=Reason("manual"), **kw)  # fmt: skip
    return r.engine.manual_order(it).client_order_id


def test_single_order_lifecycle_and_mapping(tmp_path: Path, dataset):
    async def main() -> None:
        r, ib, clock = make(tmp_path)
        await r.startup()
        assert ib.readonly is False and r.account == "DU1234567"
        coid = manual(r, 50)
        assert ib.placed == []  # 落盘之前不发
        await tick(r, ib, clock)
        ((oid, spec),) = ib.placed
        assert (spec.order_ref, spec.action, spec.qty, spec.order_type) == (coid, "BUY", 50, "MKT")
        assert spec.account == "DU1234567" and spec.transmit and spec.parent_id is None
        ib.status(oid, "Submitted")
        ib.execute(oid, 20, 100.01, "e1.01.01", commission=1.0)
        ib.execute(oid, 30, 100.03, "e2.01.01")
        await tick(r, ib, clock)
        o = r.engine.oms.orders[coid]  # type: ignore[union-attr]
        assert o.status == OrderStatus.FILLED and o.broker_order_id == oid and o.perm_id == oid * 10
        assert o.commission_pending  # e2 的手续费还没到
        ib._comm_cbs[0]("e2.01.01", 0.5)
        await tick(r, ib, clock)
        assert not o.commission_pending and o.commission == D("1.5")
        assert r.positions() == {"AAA": 50}
        # 落盘里有券商订单号（重启后可以撤单、改单）
        rec = r.journal.load()  # type: ignore[union-attr]
        assert rec.orders[coid].broker_order_id == oid
        r.close()

    run(main)


def test_native_bracket(tmp_path: Path, dataset):
    async def main() -> None:
        r, ib, clock = make(tmp_path)
        await r.startup()
        e = r.engine
        assert e is not None
        coid = manual(r, 40, order_type="LMT", limit_price=D("100"),
                      take_profit=D("101"), stop_loss=D("99"))  # fmt: skip
        await tick(r, ib, clock)
        assert [(sp.order_type, sp.transmit, sp.parent_id is not None) for _, sp in ib.placed] == [
            ("LMT", False, False), ("LMT", False, True), ("STP", True, True),
        ]  # fmt: skip
        pid = ib.id_of(coid)
        assert all(sp.parent_id == pid for _, sp in ib.placed[1:])
        tp_id, sl_id = ib.placed[1][0], ib.placed[2][0]
        for oid in (pid, tp_id, sl_id):
            ib.status(oid, "PreSubmitted" if oid != pid else "Submitted")
        # 入场单成交：保护单已经在券商端，不需要再发任何请求
        ib.execute(pid, 40, 100.0, "b1.01.01")
        ib.status(pid, "Filled", 40)
        n_before = len(ib.placed)
        await tick(r, ib, clock)
        assert r.positions() == {"AAA": 40} and len(ib.placed) == n_before and not ib.cancels
        # 止盈成交，券商按 OCA 撤销止损
        ib.execute(tp_id, 40, 101.0, "b2.01.01")
        ib.status(tp_id, "Filled", 40)
        ib.status(sl_id, "Cancelled")
        await tick(r, ib, clock)
        assert r.positions() == {}
        statuses = sorted(o.status.value for o in e.oms.orders.values())
        assert statuses == ["CANCELLED", "FILLED", "FILLED"]
        assert len(ib.placed) == n_before and not ib.cancels  # 没有多余的改单、撤单
        r.close()

    run(main)


def test_bracket_partial_entry_then_cancelled_resizes_children(tmp_path: Path, dataset):
    async def main() -> None:
        r, ib, clock = make(tmp_path)
        await r.startup()
        coid = manual(r, 40, order_type="LMT", limit_price=D("100"),
                      take_profit=D("101"), stop_loss=D("99"))  # fmt: skip
        await tick(r, ib, clock)
        pid = ib.id_of(coid)
        tp_id, sl_id = ib.placed[1][0], ib.placed[2][0]
        ib.execute(pid, 15, 100.0, "p1.01.01")
        r.engine.manual_cancel(coid)  # type: ignore[union-attr]
        await tick(r, ib, clock)
        assert pid in ib.cancels
        ib.status(pid, "Cancelled", 15)
        await tick(r, ib, clock)
        # 保护单改成 15 股（同一个 orderId 再发一次），并保留父单关系
        mods = [(oid, sp.qty, sp.parent_id) for oid, sp in ib.placed[3:]]
        assert sorted(mods) == sorted([(tp_id, 15, pid), (sl_id, 15, pid)])
        r.close()

    run(main)


def test_broker_rejections_and_messages(tmp_path: Path, dataset):
    async def main() -> None:
        r, ib, clock = make(tmp_path)
        await r.startup()
        a = manual(r, 10)
        b = manual(r, 11)
        await tick(r, ib, clock)
        ib.error(201, "Order rejected - reason: no shares to short", req_id=ib.id_of(a))
        ib.error(399, "Order will not be placed at the exchange until 09:30", req_id=ib.id_of(b))
        ib.status(ib.id_of(b), "PreSubmitted")
        await tick(r, ib, clock)
        oms = r.engine.oms  # type: ignore[union-attr]
        assert oms.orders[a].status == OrderStatus.REJECTED and "201" in oms.orders[a].reject_detail
        assert oms.orders[b].status == OrderStatus.ACCEPTED
        assert any("399" in x.detail for x in r.alerts if x.kind == "broker")
        # 改单被拒：恢复原内容
        oms.modify(b, clock.t, qty=12)
        await tick(r, ib, clock)
        assert ib.placed[-1][1].qty == 12
        ib.error(10147, "OrderId not found for cancel", req_id=ib.id_of(b))  # 只是提示
        ib.error(103, "Duplicate order id", req_id=ib.id_of(b))
        await tick(r, ib, clock)
        assert oms.orders[b].status == OrderStatus.ACCEPTED and oms.orders[b].intent.qty == 11
        r.close()

    run(main)


def test_send_timeout_then_query(tmp_path: Path, dataset):
    async def main() -> None:
        r, ib, clock = make(tmp_path)
        await r.startup()
        lost, got = manual(r, 10), manual(r, 12)
        await tick(r, ib, clock)
        # got：券商收到了而且成交了，但回报都没传回来；lost：券商确实没收到
        ib.status(ib.id_of(got), "Filled", 12, emit=False)
        ib.execute(ib.id_of(got), 12, 100.0, "q1.01.01", emit=False)
        ib.specs.pop(ib.id_of(lost))
        ib.placed = [p for p in ib.placed if p[1].order_ref != lost]
        await tick(r, ib, clock, n=11)
        oms = r.engine.oms  # type: ignore[union-attr]
        assert any("发送超时" in x.detail for x in r.alerts)
        await tick(r, ib, clock)
        assert oms.orders[lost].status == OrderStatus.REJECTED
        assert oms.orders[lost].reject_rule == "not_received"
        assert oms.orders[got].status == OrderStatus.FILLED and r.positions() == {"AAA": 12}
        r.close()

    run(main)


def test_send_failure_while_disconnected(tmp_path: Path, dataset):
    async def main() -> None:
        r, ib, clock = make(tmp_path)
        await r.startup()
        ib.place_fail = True
        coid = manual(r, 10)
        await tick(r, ib, clock)
        oms = r.engine.oms  # type: ignore[union-attr]
        # 发送失败 → UNKNOWN → 查询：券商没有 → 失败（不自动重发）
        await tick(r, ib, clock)
        assert oms.orders[coid].status == OrderStatus.REJECTED
        assert ib.placed == []
        r.close()

    run(main)


def test_fill_correction(tmp_path: Path, dataset):
    async def main() -> None:
        r, ib, clock = make(tmp_path)
        await r.startup()
        coid = manual(r, 10)
        await tick(r, ib, clock)
        oid = ib.id_of(coid)
        ib.execute(oid, 10, 100.50, "c1.01.01")
        await tick(r, ib, clock)
        ib.execute(oid, 10, 100.25, "c1.01.02")  # 更正
        await tick(r, ib, clock)
        e = r.engine
        assert e is not None
        f = e.oms.fills[-1]
        assert f.corrects == "c1.01.01"
        pos = e.portfolio.position("AAA")
        assert pos.qty == 10 and pos.avg_cost == D("100.25")
        r.close()

    run(main)


def test_rate_limit(tmp_path: Path, dataset):
    async def main() -> None:
        r, ib, clock = make(
            tmp_path,
            risk={"max_orders_per_min": 100, "max_position_usd": 1e6, "max_order_usd": 1e6},
        )  # type: ignore[arg-type]
        await r.startup()
        for i in range(25):
            manual(r, 1 + i)
        clock.t += 1
        r.step(clock.t)
        assert len(ib.placed) == 20 and r.executor.queued == 5  # type: ignore[union-attr]
        await tick(r, ib, clock, n=2)
        assert len(ib.placed) == 25
        r.close()

    run(main)


def test_crash_after_send_recovers_fill_from_broker(tmp_path: Path, dataset):
    async def main() -> None:
        r, ib, clock = make(tmp_path)
        await r.startup()
        coid = manual(r, 30)
        await tick(r, ib, clock)
        oid = ib.id_of(coid)
        r.close()  # 崩溃：之后的回报本进程都没收到
        ib.status(oid, "Filled", 30, emit=False)
        ib.execute(oid, 30, 100.2, "r1.01.01", emit=False)
        ib.broker_positions = [BrokerPosition("DU1234567", "AAA", 30, 100.2)]
        clock2 = Clock(clock.t + 30 * S)
        ib2 = fake_ib(clock2)
        ib2.states, ib2.execs, ib2.specs = ib.states, ib.execs, ib.specs
        ib2.broker_positions = ib.broker_positions
        r2, _, _ = make(tmp_path, clock2, ib2)
        await r2.startup()
        o = r2.engine.oms.orders[coid]  # type: ignore[union-attr]
        assert o.status == OrderStatus.FILLED and o.filled_qty == 30
        assert r2.positions() == {"AAA": 30}
        assert r2._mismatch == {}  # 对账一致
        assert ib2.placed == []  # 没有重发
        r2.close()

    run(main)


def test_reconcile_mismatch_pauses_and_resync(tmp_path: Path, dataset):
    async def main() -> None:
        r, ib, clock = make(tmp_path)
        await r.startup()
        # 券商有本地不知道的持仓（例如在手机上手动买的）
        ib.broker_positions = [BrokerPosition("DU1234567", "BBB", 100, 50.0)]
        found = await r.reconcile()
        assert "BBB" in found
        assert not any(a.kind == "RECONCILE_MISMATCH" for a in r.alerts)  # 第一次只记下
        await r.reconcile()
        assert any(a.kind == "RECONCILE_MISMATCH" for a in r.alerts)
        # 人工按券商持仓重置
        r.resync_to_broker({"BBB": (100, 50.0)})
        assert r.positions() == {"BBB": 100}
        assert await r.reconcile() == {}
        r.close()

    run(main)


def test_reconcile_flags_foreign_orders(tmp_path: Path, dataset):
    async def main() -> None:
        r, ib, clock = make(tmp_path)
        await r.startup()
        from trader.brokers.ibkr.api import OrderSpec

        ib.specs[999] = OrderSpec("AAA", "BUY", 5, "LMT", "", limit_price=90.0)
        ib.status(999, "Submitted")
        await tick(r, ib, clock)
        await r.reconcile()
        await r.reconcile()
        mism = [a for a in r.alerts if a.kind == "RECONCILE_MISMATCH"]
        assert mism and "不认识的挂单" in mism[0].detail
        r.close()

    run(main)


def test_mismatch_pauses_running_strategy(tmp_path: Path, dataset):
    async def main() -> None:
        clock = Clock(T0)
        ib = fake_ib(clock)
        cfg = config(profile="broker_paper")
        r = LiveRunner(cfg, ib, UNIVERSE, tmp_path, tmp_path / "runs" / "b2", clock)
        r.feed_timing = (0, 0)
        r.clock_sample_gap_s = 0
        await r.startup()
        assert r.start_strategy() is None
        ib.broker_positions = [BrokerPosition("DU1234567", "AAA", 7, 100.0)]
        await r.reconcile()
        await r.reconcile()
        assert "test_live_buy_once" in r.engine.oms.halted  # type: ignore[union-attr]
        assert [k for k in SystemKind if k.value == "RECONCILE_MISMATCH"]
        r.close()

    run(main)


def test_account_selection(tmp_path: Path, dataset):
    async def main() -> None:
        clock = Clock(T0)
        ib = fake_ib(clock, accounts=["DU1", "DU2"])
        r, _, _ = make(tmp_path, clock, ib)
        with pytest.raises(Exception, match="多个账户"):
            await r.startup()
        ib2 = fake_ib(clock, accounts=["DU1", "DU2"])
        r2, _, _ = make(tmp_path, clock, ib2, ibkr={"account": "DU2"})
        await r2.startup()
        assert r2.account == "DU2"
        r2.close()

    run(main)


def test_day_is_trading_day():
    assert DAY.weekday() < 5
