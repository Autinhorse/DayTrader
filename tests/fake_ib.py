"""假的 IB 客户端：实现 IbApi，按测试设定返回历史数据、推送逐笔和快照、模拟错误和断线。

自动化测试只用它，永远不连接真实的 IB Gateway。
"""

from __future__ import annotations

from collections.abc import Callable

from trader.brokers.ibkr.api import (
    BrokerOrderState,
    BrokerPosition,
    ExecReport,
    HistBar,
    OrderSpec,
    Snapshot,
    check_paper_port,
)
from trader.data.bar_builder import TradeTick


class FakeIb:
    def __init__(self, accounts: list[str] | None = None, server_skew_ns: int = 0) -> None:
        self.accounts = ["DU1234567"] if accounts is None else accounts
        self.connected = False
        self.port: int | None = None
        self.readonly: bool | None = None
        self.clock: Callable[[], int] = lambda: 0
        self.server_skew_ns = server_skew_ns
        self.known: set[str] | None = None  # None：全部可识别
        self.tick_subs: dict[str, Callable[[TradeTick], None]] = {}
        self.snap_subs: dict[str, Callable[[Snapshot], None]] = {}
        self.hist: dict[tuple[str, str], list[HistBar]] = {}
        self.hist_calls: list[tuple[str, str, str]] = []
        self.hist_fail: set[str] = set()
        self._err: list[Callable[[int, int, str, str | None], None]] = []
        # 下单部分：模拟券商
        self.next_id = 100
        self.placed: list[tuple[int, OrderSpec]] = []
        self.cancels: list[int] = []
        self.specs: dict[int, OrderSpec] = {}
        self.states: dict[int, BrokerOrderState] = {}
        self.execs: list[ExecReport] = []
        self.broker_positions: list[BrokerPosition] = []
        self.place_fail = False
        self._status_cbs: list[Callable[[BrokerOrderState], None]] = []
        self._exec_cbs: list[Callable[[ExecReport], None]] = []
        self._comm_cbs: list[Callable[[str, float], None]] = []
        self._disc: list[Callable[[], None]] = []

    # 连接
    async def connect(self, host: str, port: int, client_id: int, readonly: bool) -> None:
        check_paper_port(port)
        self.connected, self.port, self.readonly = True, port, readonly

    def disconnect(self) -> None:
        self.connected = False

    def is_connected(self) -> bool:
        return self.connected

    def managed_accounts(self) -> list[str]:
        return list(self.accounts)

    async def server_time(self) -> int:
        return self.clock() + self.server_skew_ns

    async def qualify(self, symbols: list[str]) -> list[str]:
        return [s for s in symbols if self.known is None or s in self.known]

    # 行情
    def subscribe_ticks(self, symbol: str, on_trade: Callable[[TradeTick], None]) -> None:
        self.tick_subs[symbol] = on_trade

    def subscribe_snapshots(self, symbol: str, on_snap: Callable[[Snapshot], None]) -> None:
        self.snap_subs[symbol] = on_snap

    def unsubscribe(self, symbol: str) -> None:
        self.tick_subs.pop(symbol, None)
        self.snap_subs.pop(symbol, None)

    async def historical_bars(
        self, symbol: str, duration: str, bar_size: str, end: int | None = None
    ) -> list[HistBar]:
        self.hist_calls.append((symbol, duration, bar_size))
        if symbol in self.hist_fail:
            raise RuntimeError("pacing violation")
        return list(self.hist.get((symbol, bar_size), []))

    def on_error(self, cb: Callable[[int, int, str, str | None], None]) -> None:
        self._err.append(cb)

    def on_disconnect(self, cb: Callable[[], None]) -> None:
        self._disc.append(cb)

    # 测试操作
    def trade(self, t: TradeTick) -> None:
        self.tick_subs[t.symbol](t)

    def snap(self, s: Snapshot) -> None:
        self.snap_subs[s.symbol](s)

    def error(self, code: int, msg: str = "", symbol: str | None = None, req_id: int = -1) -> None:
        for cb in self._err:
            cb(req_id, code, msg, symbol)

    def drop(self) -> None:
        self.connected = False
        for cb in self._disc:
            cb()

    # ---------- 下单（模拟券商） ----------

    def next_order_id(self) -> int:
        self.next_id += 1
        return self.next_id

    def place_order(self, order_id: int, spec: OrderSpec) -> None:
        if not self.connected or self.place_fail:
            raise ConnectionError("not connected")
        self.placed.append((order_id, spec))
        self.specs[order_id] = spec

    def cancel_order(self, order_id: int) -> None:
        self.cancels.append(order_id)

    async def open_orders(self) -> list[BrokerOrderState]:
        done = ("Cancelled", "ApiCancelled", "Filled", "Inactive")
        return [s for s in self.states.values() if s.status not in done]

    async def executions(self) -> list[ExecReport]:
        return list(self.execs)

    async def positions(self) -> list[BrokerPosition]:
        return list(self.broker_positions)

    def on_order_status(self, cb: Callable[[BrokerOrderState], None]) -> None:
        self._status_cbs.append(cb)

    def on_execution(self, cb: Callable[[ExecReport], None]) -> None:
        self._exec_cbs.append(cb)

    def on_commission(self, cb: Callable[[str, float], None]) -> None:
        self._comm_cbs.append(cb)

    # 测试操作：券商回报
    def status(self, order_id: int, status: str, filled: float = 0, emit: bool = True) -> None:
        sp = self.specs[order_id]
        st = BrokerOrderState(order_id, order_id * 10, sp.order_ref, sp.symbol, status,
                              filled, sp.qty - filled)  # fmt: skip
        self.states[order_id] = st
        if emit:
            for cb in self._status_cbs:
                cb(st)

    def execute(
        self, order_id: int, qty: float, price: float, exec_id: str, ts: int = 0,
        commission: float | None = None, emit: bool = True,
    ) -> None:  # fmt: skip
        sp = self.specs[order_id]
        side = "BOT" if sp.action == "BUY" else "SLD"
        e = ExecReport(exec_id, order_id, order_id * 10, sp.order_ref, sp.symbol, side, qty, price,
                       ts or self.clock())  # fmt: skip
        self.execs.append(e)
        if emit:
            for cb in self._exec_cbs:
                cb(e)
            if commission is not None:
                for c in self._comm_cbs:
                    c(exec_id, commission)

    def id_of(self, order_ref: str) -> int:
        return next(oid for oid, sp in self.placed if sp.order_ref == order_ref)
