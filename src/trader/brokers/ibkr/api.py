"""IBKR 连接的薄接口（DESIGN.md 5.5、9.3）。

行情源和执行器只通过 IbApi 使用 IBKR；真实实现 IbAsyncApi 包装 ib_async，测试用假的实现回放消息。
这样自动化测试永远不连接 IBKR。

安全规则（阶段 6）：只允许模拟端口（Gateway 4002、TWS 7497），
连接后账户号必须以 DU 开头（模拟账户）。
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from trader.data.bar_builder import TradeTick

PAPER_PORTS = frozenset({4002, 7497})
LIVE_PORTS = frozenset({4001, 7496})


class SafetyError(RuntimeError):
    """违反实盘安全规则：拒绝连接或立即断开。"""


def check_paper_port(port: int) -> None:
    if port not in PAPER_PORTS:
        raise SafetyError(
            f"端口 {port} 不是模拟账户端口 {sorted(PAPER_PORTS)}；阶段 6 只允许连接模拟账户"
        )


def check_paper_accounts(accounts: list[str]) -> None:
    if not accounts:
        raise SafetyError("连接后没有拿到账户号，无法确认是模拟账户")
    bad = [a for a in accounts if not a.startswith("DU")]
    if bad:
        raise SafetyError(f"账户 {bad} 不是模拟账户（模拟账户以 DU 开头），已断开")


@dataclass(frozen=True, slots=True)
class HistBar:
    ts: int  # 区间开始，UTC 纳秒
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float | None
    trades: int | None


@dataclass(frozen=True, slots=True)
class Snapshot:
    symbol: str
    recv: int
    last: float | None
    volume: float | None  # 当天累计成交量
    bid: float | None = None
    ask: float | None = None


class IbApi(Protocol):
    async def connect(self, host: str, port: int, client_id: int, readonly: bool) -> None: ...
    def disconnect(self) -> None: ...
    def is_connected(self) -> bool: ...
    def managed_accounts(self) -> list[str]: ...
    async def server_time(self) -> int: ...
    async def qualify(self, symbols: list[str]) -> list[str]: ...
    def subscribe_ticks(self, symbol: str, on_trade: Callable[[TradeTick], None]) -> None: ...
    def subscribe_snapshots(self, symbol: str, on_snap: Callable[[Snapshot], None]) -> None: ...
    def unsubscribe(self, symbol: str) -> None: ...
    async def historical_bars(
        self, symbol: str, duration: str, bar_size: str, end: int | None = None
    ) -> list[HistBar]: ...
    def on_error(self, cb: Callable[[int, str, str | None], None]) -> None: ...
    def on_disconnect(self, cb: Callable[[], None]) -> None: ...


def _num(x: Any) -> float | None:
    if x is None:
        return None
    try:
        return None if math.isnan(x) else float(x)
    except (TypeError, ValueError):
        return None


class IbAsyncApi:
    """ib_async 实现。行情部分只读；下单部分在执行器里（broker_paper）。"""

    def __init__(self, now: Callable[[], int]) -> None:
        """now：本机时间（纳秒），由调用方从 trader.core.clock 传入。"""
        from ib_async import IB

        self.ib = IB()
        self._now = now
        self._contracts: dict[str, Any] = {}
        self._tick_cb: dict[int, tuple[str, Callable[[TradeTick], None]]] = {}
        self._snap_cb: dict[int, tuple[str, Callable[[Snapshot], None]]] = {}
        self._tickers: dict[str, Any] = {}
        self._hooked = False

    async def connect(self, host: str, port: int, client_id: int, readonly: bool) -> None:
        from ib_async.ib import StartupFetch

        check_paper_port(port)
        fetch = StartupFetch(0) if readonly else StartupFetch.POSITIONS | StartupFetch.ORDERS_OPEN
        await self.ib.connectAsync(
            host, port, clientId=client_id, readonly=readonly, fetchFields=fetch
        )
        self.ib.reqMarketDataType(1)
        if not self._hooked:
            self._hook()
            self.ib.pendingTickersEvent += self._on_pending
            self._hooked = True

    def disconnect(self) -> None:
        self.ib.disconnect()

    def is_connected(self) -> bool:
        return self.ib.isConnected()

    def managed_accounts(self) -> list[str]:
        return list(self.ib.managedAccounts())

    async def server_time(self) -> int:
        dt = await self.ib.reqCurrentTimeAsync()
        return int(dt.timestamp() * 1e9)

    async def qualify(self, symbols: list[str]) -> list[str]:
        from ib_async import Stock

        todo = [Stock(s, "SMART", "USD") for s in symbols if s not in self._contracts]
        if todo:
            for c in await self.ib.qualifyContractsAsync(*todo):
                if c is None or isinstance(c, list):  # 无法识别或有歧义
                    continue
                if c.conId:
                    self._contracts[c.symbol] = c
        return [s for s in symbols if s in self._contracts]

    def _hook(self) -> None:
        """ib_async 会把逐笔成交的 IB 原始时间换成本机接收时间，所以截获原始回调。

        与 tools/ib_record.py 的做法相同。
        """
        wrapper = self.ib.wrapper
        original = wrapper.tickByTickAllLast

        def hooked(reqId, tickType, time, price, size, attrib, exchange, conditions):  # noqa: ANN001, N803
            ticker = wrapper.reqId2Ticker.get(reqId)
            entry = self._tick_cb.get(id(ticker)) if ticker is not None else None
            if entry is not None:
                sym, cb = entry
                cb(
                    TradeTick(
                        sym,
                        int(time) * 1_000_000_000,
                        float(price),
                        float(size),
                        self._now(),
                        bool(getattr(attrib, "unreported", False)),
                        conditions or "",
                        exchange or "",
                    )
                )
            original(reqId, tickType, time, price, size, attrib, exchange, conditions)

        wrapper.tickByTickAllLast = hooked  # type: ignore[method-assign]  # pyright: ignore[reportAttributeAccessIssue]

    def subscribe_ticks(self, symbol: str, on_trade: Callable[[TradeTick], None]) -> None:
        t = self.ib.reqTickByTickData(self._contracts[symbol], "AllLast")
        self._tickers[symbol] = t
        self._tick_cb[id(t)] = (symbol, on_trade)

    def subscribe_snapshots(self, symbol: str, on_snap: Callable[[Snapshot], None]) -> None:
        t = self.ib.reqMktData(self._contracts[symbol], "233")
        self._tickers[symbol] = t
        self._snap_cb[id(t)] = (symbol, on_snap)

    def unsubscribe(self, symbol: str) -> None:
        t = self._tickers.pop(symbol, None)
        if t is None:
            return
        c = self._contracts[symbol]
        if self._tick_cb.pop(id(t), None) is not None:
            self.ib.cancelTickByTickData(c, "AllLast")
        if self._snap_cb.pop(id(t), None) is not None:
            self.ib.cancelMktData(c)

    def _on_pending(self, tickers: set[Any]) -> None:
        recv = self._now()
        for t in tickers:
            entry = self._snap_cb.get(id(t))
            if entry is None:
                continue
            sym, cb = entry
            cb(Snapshot(sym, recv, _num(t.last), _num(t.volume), _num(t.bid), _num(t.ask)))

    async def historical_bars(
        self, symbol: str, duration: str, bar_size: str, end: int | None = None
    ) -> list[HistBar]:
        from datetime import UTC, datetime

        end_dt = "" if end is None else datetime.fromtimestamp(end / 1e9, tz=UTC)
        bars = await self.ib.reqHistoricalDataAsync(
            self._contracts[symbol],
            endDateTime=end_dt,
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow="TRADES",
            useRTH=False,
            formatDate=2,
        )
        out = []
        for b in bars or []:
            d = b.date
            if not isinstance(d, datetime):  # 日线以上才返回 date，这里用不到
                d = datetime(d.year, d.month, d.day, tzinfo=UTC)
            ts = int(d.timestamp() * 1e9)
            vwap = _num(getattr(b, "average", None))
            n = getattr(b, "barCount", None)
            out.append(
                HistBar(
                    ts,
                    b.open,
                    b.high,
                    b.low,
                    b.close,
                    float(b.volume),
                    vwap,
                    n if n and n > 0 else None,
                )
            )
        return out

    def on_error(self, cb: Callable[[int, str, str | None], None]) -> None:
        def handler(req_id: int, code: int, msg: str, contract: Any) -> None:
            cb(code, msg, getattr(contract, "symbol", None) if contract else None)

        self.ib.errorEvent += handler

    def on_disconnect(self, cb: Callable[[], None]) -> None:
        self.ib.disconnectedEvent += cb
