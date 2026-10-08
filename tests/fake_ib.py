"""假的 IB 客户端：实现 IbApi，按测试设定返回历史数据、推送逐笔和快照、模拟错误和断线。

自动化测试只用它，永远不连接真实的 IB Gateway。
"""

from __future__ import annotations

from collections.abc import Callable

from trader.brokers.ibkr.api import HistBar, Snapshot, check_paper_port
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
        self._err: list[Callable[[int, str, str | None], None]] = []
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

    def on_error(self, cb: Callable[[int, str, str | None], None]) -> None:
        self._err.append(cb)

    def on_disconnect(self, cb: Callable[[], None]) -> None:
        self._disc.append(cb)

    # 测试操作
    def trade(self, t: TradeTick) -> None:
        self.tick_subs[t.symbol](t)

    def snap(self, s: Snapshot) -> None:
        self.snap_subs[s.symbol](s)

    def error(self, code: int, msg: str = "", symbol: str | None = None) -> None:
        for cb in self._err:
            cb(code, msg, symbol)

    def drop(self) -> None:
        self.connected = False
        for cb in self._disc:
            cb()
