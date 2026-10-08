"""交易日志与崩溃恢复（DESIGN.md 8.2、12.4），实盘版（local_paper / broker_paper / live）使用。

落盘顺序：
1. 输入事件（券商回报、定时器、交易时段、人工操作）到达 → record_input() 立即落盘，标记“未处理”；
2. 引擎处理它（归约、回调策略），策略可能下单、改 ctx.state；
3. commit()：订单快照、新成交、手续费、标的归属、ctx.state、“该输入已处理”
   放在同一个 SQLite 事务里提交（WAL 模式，提交时同步刷盘）；
4. 提交成功后订单管理模块才把发件箱里的请求发给券商；
5. 最后把这一步追加到 events.jsonl（审计和录制重放用，可以滞后；恢复只依赖 SQLite）。

恢复（recover_into）：从 SQLite 读回订单、成交（重建持仓）、手续费、标的归属和各策略的 ctx.state；
已落盘但没有收到任何券商回报的订单置为 UNKNOWN，之后向券商查询，不直接重发；
未处理的输入事件按原顺序返回，由引擎重新交给策略（每个只处理一次）。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from trader.core.models import CommissionUpdate, Fill, Order, OrderIntent, OrderStatus, Reason
from trader.oms.manager import OrderManager
from trader.oms.orders import SendTimeout, reduce, reduce_commission

_SCHEMA = """
CREATE TABLE IF NOT EXISTS inputs (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    processed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS orders (
    coid TEXT PRIMARY KEY,
    snapshot TEXT NOT NULL,
    updated INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS fills (
    exec_id TEXT PRIMARY KEY,
    coid TEXT NOT NULL,
    ts INTEGER NOT NULL,
    price TEXT NOT NULL,
    qty INTEGER NOT NULL,
    corrects TEXT
);
CREATE TABLE IF NOT EXISTS commissions (
    exec_id TEXT PRIMARY KEY,
    amount TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS owners (
    symbol TEXT PRIMARY KEY,
    owner TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_state (
    strategy_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    updated INTEGER NOT NULL
);
"""


# ---------- 序列化 ----------


def _dec(x: Decimal | None) -> str | None:
    return None if x is None else str(x)


def _undec(x: str | None) -> Decimal | None:
    return None if x is None else Decimal(x)


def intent_to_dict(it: OrderIntent) -> dict[str, Any]:
    return {
        "source": it.source,
        "symbol": it.symbol,
        "side": it.side,
        "qty": it.qty,
        "order_type": it.order_type,
        "reason": {"code": it.reason.code, "text": it.reason.text, "context": it.reason.context},
        "limit_price": _dec(it.limit_price),
        "stop_price": _dec(it.stop_price),
        "tif": it.tif,
        "outside_rth": it.outside_rth,
        "take_profit": _dec(it.take_profit),
        "stop_loss": _dec(it.stop_loss),
        "tags": list(it.tags),
    }


def intent_from_dict(d: dict[str, Any]) -> OrderIntent:
    r = d["reason"]
    return OrderIntent(
        source=d["source"],
        symbol=d["symbol"],
        side=d["side"],
        qty=d["qty"],
        order_type=d["order_type"],
        reason=Reason(r["code"], r.get("text", ""), r.get("context", {})),
        limit_price=_undec(d.get("limit_price")),
        stop_price=_undec(d.get("stop_price")),
        tif=d.get("tif", "DAY"),
        outside_rth=d.get("outside_rth", False),
        take_profit=_undec(d.get("take_profit")),
        stop_loss=_undec(d.get("stop_loss")),
        tags=tuple(d.get("tags", ())),
    )


def order_to_dict(o: Order) -> dict[str, Any]:
    """订单快照（成交明细单独存在 fills 表，恢复时重放）。"""
    return {
        "intent": intent_to_dict(o.intent),
        "client_order_id": o.client_order_id,
        "status": o.status.value,
        "account": o.account,
        "client_id": o.client_id,
        "broker_order_id": o.broker_order_id,
        "perm_id": o.perm_id,
        "created_at": o.created_at,
        "parent_id": o.parent_id,
        "oca_group": o.oca_group,
        "role": o.role,
        "reject_rule": o.reject_rule,
        "reject_detail": o.reject_detail,
        "broker_status": o.broker_status,
        "pending_intent": intent_to_dict(o.pending_intent) if o.pending_intent else None,
        "status_times": {k.value: v for k, v in o.status_times.items()},
        "notes": o.notes,
    }


def order_from_dict(d: dict[str, Any]) -> Order:
    o = Order(intent=intent_from_dict(d["intent"]), client_order_id=d["client_order_id"])
    o.status = OrderStatus(d["status"])
    o.account, o.client_id = d.get("account"), d.get("client_id")
    o.broker_order_id, o.perm_id = d.get("broker_order_id"), d.get("perm_id")
    o.created_at = d.get("created_at", 0)
    o.parent_id, o.oca_group, o.role = (
        d.get("parent_id"),
        d.get("oca_group"),
        d.get("role", "single"),
    )
    o.reject_rule, o.reject_detail = d.get("reject_rule"), d.get("reject_detail", "")
    o.broker_status = d.get("broker_status")
    pi = d.get("pending_intent")
    o.pending_intent = intent_from_dict(pi) if pi else None
    o.status_times = {OrderStatus(k): v for k, v in d.get("status_times", {}).items()}
    o.notes = list(d.get("notes", []))
    return o


# ---------- 日志 ----------


@dataclass
class Recovered:
    orders: dict[str, Order]
    fills: list[Fill]
    commissions: list[CommissionUpdate]
    owners: dict[str, str]
    states: dict[str, dict[str, Any]]
    pending_inputs: list[tuple[int, int, str, dict[str, Any]]] = field(default_factory=list)


class Journal:
    def __init__(self, db_path: Path, jsonl_path: Path | None = None) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")  # 提交时同步刷盘
        self.db.executescript(_SCHEMA)
        self.jsonl_path = jsonl_path
        if jsonl_path is not None:
            jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    def close(self) -> None:
        self.db.close()

    def record_input(self, ts: int, kind: str, payload: dict[str, Any]) -> int:
        """输入事件立即落盘（未处理），返回序号。"""
        cur = self.db.execute(
            "INSERT INTO inputs (ts, kind, payload) VALUES (?, ?, ?)",
            (ts, kind, json.dumps(payload, ensure_ascii=False, default=str)),
        )
        assert cur.lastrowid is not None
        return cur.lastrowid

    def commit(
        self,
        ts: int,
        *,
        processed: int | None = None,
        orders: Iterable[Order] = (),
        fills: Iterable[Fill] = (),
        commissions: Iterable[CommissionUpdate] = (),
        owners: dict[str, str] | None = None,
        states: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """一步的全部变化放进同一个事务；失败则全部回滚（发件箱不会发出）。"""
        orders, fills, commissions = list(orders), list(fills), list(commissions)
        db = self.db
        db.execute("BEGIN IMMEDIATE")
        try:
            for o in orders:
                db.execute(
                    "INSERT OR REPLACE INTO orders VALUES (?, ?, ?)",
                    (o.client_order_id, json.dumps(order_to_dict(o), ensure_ascii=False), ts),
                )
            for f in fills:
                db.execute(
                    "INSERT OR IGNORE INTO fills VALUES (?, ?, ?, ?, ?, ?)",
                    (f.broker_exec_id, f.client_order_id, f.ts, str(f.price), f.qty, f.corrects),
                )
            for c in commissions:
                db.execute(
                    "INSERT OR IGNORE INTO commissions VALUES (?, ?)",
                    (c.broker_exec_id, str(c.commission)),
                )
            if owners is not None:
                db.execute("DELETE FROM owners")
                db.executemany("INSERT INTO owners VALUES (?, ?)", list(owners.items()))
            for sid, st in (states or {}).items():
                db.execute(
                    "INSERT OR REPLACE INTO strategy_state VALUES (?, ?, ?)",
                    (sid, json.dumps(st, ensure_ascii=False), ts),
                )
            if processed is not None:
                db.execute("UPDATE inputs SET processed = 1 WHERE seq = ?", (processed,))
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        self._append_jsonl(ts, processed, orders, fills, commissions)

    def _append_jsonl(
        self,
        ts: int,
        processed: int | None,
        orders: list[Order],
        fills: list[Fill],
        commissions: list[CommissionUpdate],
    ) -> None:
        if self.jsonl_path is None or not (orders or fills or commissions or processed):
            return
        rec = {
            "ts": ts,
            "input": processed,
            "orders": [
                {"coid": o.client_order_id, "status": o.status.value, "filled": o.filled_qty}
                for o in orders
            ],
            "fills": [
                {
                    "exec": f.broker_exec_id,
                    "coid": f.client_order_id,
                    "price": str(f.price),
                    "qty": f.qty,
                }
                for f in fills
            ],
            "commissions": [
                {"exec": c.broker_exec_id, "amount": str(c.commission)} for c in commissions
            ],
        }
        with self.jsonl_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def load(self) -> Recovered:
        db = self.db
        orders = {
            coid: order_from_dict(json.loads(snap))
            for coid, snap in db.execute("SELECT coid, snapshot FROM orders")
        }
        fills = [
            Fill(coid, ts, Decimal(price), qty, exec_id, corrects)
            for exec_id, coid, ts, price, qty, corrects in db.execute(
                "SELECT exec_id, coid, ts, price, qty, corrects FROM fills ORDER BY rowid"
            )
        ]
        commissions = [
            CommissionUpdate(e, Decimal(a))
            for e, a in db.execute("SELECT exec_id, amount FROM commissions")
        ]
        owners = dict(db.execute("SELECT symbol, owner FROM owners").fetchall())
        states = {
            sid: json.loads(st)
            for sid, st in db.execute("SELECT strategy_id, state FROM strategy_state")
        }
        pending = [
            (seq, ts, kind, json.loads(payload))
            for seq, ts, kind, payload in db.execute(
                "SELECT seq, ts, kind, payload FROM inputs WHERE processed = 0 ORDER BY seq"
            )
        ]
        return Recovered(orders, fills, commissions, owners, states, pending)


def recover_into(oms: OrderManager, rec: Recovered, now: int) -> list[str]:
    """把落盘的状态装回订单管理模块，返回需要向券商查询的订单号（UNKNOWN）。

    持仓由成交按落盘顺序重放得到（不信任任何缓存的持仓数字）；手续费逐笔重放。
    重放时直接归约，不经过 OrderManager._apply：不回调策略、不重复报告异常、
    不触发括号单子单的数量调整（子单的状态以快照为准）。
    """
    for o in rec.orders.values():
        o.fills, o.exec_ids, o.filled_qty, o.avg_fill_price = {}, set(), 0, None
        o.commission, o.commission_execs = Decimal(0), set()
        oms.orders[o.client_order_id] = o
    oms.owners.update(rec.owners)
    pf = oms.portfolio
    for f in rec.fills:
        order = oms.orders.get(f.client_order_id)
        if order is None or f.broker_exec_id in oms.exec_to_order:
            continue
        status, notes = order.status, list(order.notes)
        old = None
        if f.corrects is not None:
            old = next((x for x in order.fills.values() if x.broker_exec_id == f.corrects), None)
        reduce(order, f)
        order.status, order.notes = status, notes  # 状态和备注以快照为准
        oms.exec_to_order[f.broker_exec_id] = order.client_order_id
        oms.fills.append(f)
        sym, side = order.intent.symbol, order.intent.side
        if old is not None:
            pf.apply_fill(sym, "SELL" if side == "BUY" else "BUY", old.qty, old.price)
        pf.apply_fill(sym, side, f.qty, f.price)
    for c in rec.commissions:
        base = c.broker_exec_id.split(":")[0]
        coid = oms.exec_to_order.get(base)
        if coid is None:
            continue
        added = reduce_commission(oms.orders[coid], c)
        if added:
            oms.exec_commission[base] = oms.exec_commission.get(base, Decimal(0)) + added
            pf.apply_commission(added)
    to_query = []
    for o in oms.orders.values():
        oms._notes_seen[o.client_order_id] = len(o.notes)
        if o.status in (OrderStatus.NEW, OrderStatus.SUBMITTED, OrderStatus.UNKNOWN):
            # 已落盘但没有收到券商的任何回报：不知道券商是否收到，查询后再定，不直接重发
            reduce(o, SendTimeout(o.client_order_id, now))
            to_query.append(o.client_order_id)
        elif o.status == OrderStatus.AWAITING_CONFIRM:
            o.status = OrderStatus.CANCELLED  # 等待确认中的信号一律作废（DESIGN.md 12.4）
            o.notes.append("重启后作废等待确认的信号")
            oms._notes_seen[o.client_order_id] = len(o.notes)
    return to_query
