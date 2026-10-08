"""落盘与崩溃恢复（DESIGN.md 8.2、12.4）。

模拟在各个时刻“崩溃”，重新打开日志恢复，检查订单、持仓、手续费和状态。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from tests.test_order_reduce import NOW, buy, fill, setup

from trader.core.models import CommissionUpdate, OrderStatus
from trader.oms.journal import Journal, order_from_dict, order_to_dict, recover_into
from trader.oms.orders import Accepted, Cancelled, QueryResult

D = Decimal


def _commit_all(j: Journal, m, processed: int | None = None, states=None) -> None:  # noqa: ANN001
    j.commit(
        NOW,
        processed=processed,
        orders=m.orders.values(),
        fills=m.fills,
        commissions=[CommissionUpdate(e, a) for e, a in m.exec_commission.items()],
        owners=m.owners,
        states=states,
    )


def test_order_snapshot_roundtrip():
    m, _, _ = setup()
    o = buy(m, qty=30, take_profit=D("110"), stop_loss=D("95"))
    o.notes.append("x")
    back = order_from_dict(order_to_dict(o))
    assert back.intent == o.intent and back.status == o.status and back.notes == ["x"]
    assert back.status_times == o.status_times


def test_recover_positions_commissions_and_state(tmp_path: Path):
    j = Journal(tmp_path / "j.sqlite", tmp_path / "events.jsonl")
    m, _, _ = setup()
    o = buy(m)
    c = o.client_order_id
    seq = j.record_input(NOW, "broker", {"what": "fills"})
    m.handle([Accepted(c, NOW), fill(c, 60, "100", "e1"), fill(c, 40, "101", "e2")])
    m.handle([CommissionUpdate("e1", D("0.30"))])
    _commit_all(j, m, processed=seq, states={"s": {"entered_today": True}})
    j.close()

    j2 = Journal(tmp_path / "j.sqlite")
    rec = j2.load()
    m2, _, _ = setup()
    m2.orders.clear()
    to_query = recover_into(m2, rec, NOW + 5)
    o2 = m2.orders[c]
    assert o2.status == OrderStatus.FILLED and o2.filled_qty == 100
    assert o2.avg_fill_price == D("100.4")
    assert o2.commission == D("0.30") and o2.commission_pending  # e2 的手续费还没到
    pos = m2.portfolio.position("X")
    assert pos.qty == 100 and pos.avg_cost == D("100.4")
    assert m2.portfolio.commission == D("0.30")
    assert rec.states == {"s": {"entered_today": True}}
    assert rec.pending_inputs == [] and to_query == []
    # 恢复之后到达的重复成交、迟到手续费照常处理
    m2.handle([fill(c, 40, "101", "e2"), CommissionUpdate("e2", D("0.20"))])
    assert m2.portfolio.position("X").qty == 100 and not o2.commission_pending
    assert (tmp_path / "events.jsonl").read_text(encoding="utf-8").count("\n") == 1


def test_crash_before_send_marks_unknown_and_does_not_resend(tmp_path: Path):
    """已落盘、发件箱还没发出就崩溃：恢复后订单为 UNKNOWN，先查询，不重发。"""
    j = Journal(tmp_path / "j.sqlite")
    m, venue, _ = setup(deferred=True)
    seq = j.record_input(NOW, "bar", {"symbol": "X"})
    o = buy(m)
    _commit_all(j, m, processed=seq)
    # 崩溃：m.flush() 没有执行
    assert venue.sent == []
    rec = j.load()
    m2, venue2, _ = setup(deferred=True)
    to_query = recover_into(m2, rec, NOW + 1)
    assert to_query == [o.client_order_id]
    assert m2.orders[o.client_order_id].status == OrderStatus.UNKNOWN
    assert m2.flush() == 0 and venue2.sent == []
    m2.handle([QueryResult(o.client_order_id, NOW + 2, found=False)])
    assert m2.orders[o.client_order_id].status == OrderStatus.REJECTED


def test_crash_mid_processing_returns_unprocessed_inputs(tmp_path: Path):
    """输入已落盘但处理结果没提交（崩溃在处理途中）：恢复后这个输入要重新处理一次。"""
    j = Journal(tmp_path / "j.sqlite")
    s1 = j.record_input(NOW, "bar", {"n": 1})
    j.commit(NOW, processed=s1)
    j.record_input(NOW + 1, "bar", {"n": 2})
    j.record_input(NOW + 2, "timer", {"n": 3})
    rec = j.load()
    assert [(k, p["n"]) for _, _, k, p in rec.pending_inputs] == [("bar", 2), ("timer", 3)]


def test_cancelled_with_late_fill_survives_recovery(tmp_path: Path):
    j = Journal(tmp_path / "j.sqlite")
    m, _, _ = setup()
    o = buy(m)
    c = o.client_order_id
    m.handle([Accepted(c, NOW), Cancelled(c, NOW), fill(c, 20, "100", "e9")])
    _commit_all(j, m)
    m2, _, system = setup()
    recover_into(m2, j.load(), NOW)
    o2 = m2.orders[c]
    assert o2.status == OrderStatus.CANCELLED and o2.filled_qty == 20
    assert m2.portfolio.position("X").qty == 20
    assert system == []  # 恢复不重复报告已报告过的异常
    assert sum("之后又收到成交" in n for n in o2.notes) == 1


def test_failed_commit_rolls_back(tmp_path: Path):
    j = Journal(tmp_path / "j.sqlite")
    m, _, _ = setup()
    buy(m)

    class Boom:
        client_order_id = "boom"

    with pytest.raises(AttributeError):
        j.commit(NOW, orders=[*m.orders.values(), Boom()])  # type: ignore[list-item]
    assert j.load().orders == {}
