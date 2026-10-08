"""实时 1 秒 bar 生成（DESIGN.md 5.5、6.1）：逐笔成交或行情快照 → 1 秒 bar，由时钟封口。

逐笔成交（TickBarBuilder）：
- 价格规则沿用阶段 1 的实测结论：IB 标记为 unreported 的成交（主要是场外碎股）
  不更新开高低收，只计入成交量；其余成交全部更新。
  只有 unreported 成交的那一秒没有 bar（与离线的 bars_from_trades 相同）。
- IB 逐笔成交时间只精确到秒，所以按成交时间所在的秒分桶。
- **封口**：秒 S 在本机时间到达 S + 1 秒 + 宽限 时封口（seal(now)），之后才交给引擎。
  2026-10-06 的录制显示，按 300 毫秒宽限约 10% 的成交会迟到，1000 毫秒时只有 0.05%，
  所以实时默认宽限 1000 毫秒（决策 0007）。
- **迟到成交**：封口后才到的成交不改已经发出的 bar；记入 revisions（修订后的 bar），
  带 late_revision 标记。

行情快照（SnapshotBarBuilder）：
- 按本机接收时间分秒；只有最新价或累计成交量变化的快照才算一次观测（单纯的买卖价变化不算）；
  成交量 = 累计成交量的差值。由快照生成的 bar 一律标记 degraded（最高最低价常被低估）。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

from trader.core.models import Bar
from trader.core.timeutil import NS_PER_SEC
from trader.core.trading_calendar import TradingCalendar

DEFAULT_LIVE_GRACE_MS = 1000
_KEEP_SEALED_S = 30  # 封口后保留多少秒用于迟到修订


@dataclass(frozen=True, slots=True)
class TradeTick:
    symbol: str
    ts: int  # 成交时间（IB 逐笔精确到秒）
    price: float
    size: float
    recv: int  # 本机接收时间
    unreported: bool = False
    conditions: str = ""
    exchange: str = ""


@dataclass(frozen=True, slots=True)
class BuiltBar:
    bar: Bar
    sealed_at: int
    flags: frozenset[str] = frozenset()


@dataclass(slots=True)
class _Acc:
    open: float | None = None
    high: float = float("-inf")
    low: float = float("inf")
    close: float | None = None
    volume: float = 0.0
    pv: float = 0.0
    trades: int = 0

    def add(self, price: float, size: float, updates_price: bool) -> None:
        self.volume += size
        self.pv += price * size
        self.trades += 1
        if updates_price:
            if self.open is None:
                self.open = price
            self.high = max(self.high, price)
            self.low = min(self.low, price)
            self.close = price

    def bar(self, symbol: str, ts_start: int, session: str | None) -> Bar | None:
        if self.open is None or self.close is None:
            return None
        vwap = self.pv / self.volume if self.volume > 0 else None
        return Bar(
            symbol, "1s", ts_start, self.open, self.high, self.low, self.close,
            self.volume, vwap, self.trades, session, ts_start + NS_PER_SEC,
        )  # fmt: skip


@dataclass
class TickBarBuilder:
    symbol: str
    cal: TradingCalendar
    grace_ns: int = DEFAULT_LIVE_GRACE_MS * 10**6
    start_ns: int = 0  # 交接边界：早于这一秒的成交属于历史补数，丢弃
    _open: dict[int, _Acc] = field(default_factory=dict)
    _sealed: dict[int, tuple[_Acc, str | None]] = field(default_factory=dict)
    sealed_until: int = 0  # 小于它的秒都已封口
    revisions: list[BuiltBar] = field(default_factory=list)
    late_ticks: int = 0
    last_recv: int = 0

    def add(self, t: TradeTick) -> None:
        self.last_recv = max(self.last_recv, t.recv)
        sec = t.ts // NS_PER_SEC * NS_PER_SEC
        if sec < self.start_ns:
            return
        if sec < self.sealed_until:
            self._late(sec, t)
            return
        self._open.setdefault(sec, _Acc()).add(t.price, t.size, not t.unreported)

    def _late(self, sec: int, t: TradeTick) -> None:
        self.late_ticks += 1
        kept = self._sealed.get(sec)
        if kept is None:
            acc, session = _Acc(), self.cal.session_of(sec)
            self._sealed[sec] = (acc, session)
        else:
            acc, session = kept
        acc.add(t.price, t.size, not t.unreported)
        if session is not None and (bar := acc.bar(self.symbol, sec, session)) is not None:
            self.revisions.append(BuiltBar(bar, t.recv, frozenset({"late_revision"})))

    def seal(self, now: int) -> list[BuiltBar]:
        """封口所有 秒末 + 宽限 <= now 的秒，按时间顺序返回生成的 bar（不在交易时段内的丢弃）。"""
        cutoff = (now - self.grace_ns) // NS_PER_SEC * NS_PER_SEC - NS_PER_SEC  # 最后可封口的秒
        if cutoff + NS_PER_SEC <= self.sealed_until:
            return []
        out: list[BuiltBar] = []
        for sec in sorted(s for s in self._open if s <= cutoff):
            acc = self._open.pop(sec)
            session = self.cal.session_of(sec)
            self._sealed[sec] = (acc, session)
            if session is None:
                continue
            bar = acc.bar(self.symbol, sec, session)
            if bar is not None:
                out.append(BuiltBar(bar, now))
        self.sealed_until = cutoff + NS_PER_SEC
        horizon = self.sealed_until - _KEEP_SEALED_S * NS_PER_SEC
        for sec in [s for s in self._sealed if s < horizon]:
            del self._sealed[sec]
        return out


@dataclass
class SnapshotBarBuilder:
    """快照行情 → 降级的 1 秒 bar（按接收时间分秒）。"""

    symbol: str
    cal: TradingCalendar
    grace_ns: int = DEFAULT_LIVE_GRACE_MS * 10**6
    start_ns: int = 0
    _open: dict[int, _Acc] = field(default_factory=dict)
    sealed_until: int = 0
    _last: float | None = None
    _cum: float | None = None
    last_recv: int = 0

    def add(self, recv: int, last: float | None, cum_volume: float | None) -> None:
        self.last_recv = max(self.last_recv, recv)
        price_changed = last is not None and last != self._last
        vol = 0.0
        if cum_volume is not None:
            if self._cum is not None and cum_volume > self._cum:
                vol = cum_volume - self._cum
            if self._cum is None or cum_volume > self._cum:
                self._cum = cum_volume
        if last is not None:
            self._last = last
        if not price_changed and vol == 0:
            return  # 只有买卖价变化：不是一次成交观测
        sec = recv // NS_PER_SEC * NS_PER_SEC
        if sec < self.start_ns or sec < self.sealed_until or self._last is None:
            return
        acc = self._open.setdefault(sec, _Acc())
        acc.add(self._last, vol, True)

    def seal(self, now: int) -> list[BuiltBar]:
        cutoff = (now - self.grace_ns) // NS_PER_SEC * NS_PER_SEC - NS_PER_SEC
        out: list[BuiltBar] = []
        for sec in sorted(s for s in self._open if s <= cutoff):
            acc = self._open.pop(sec)
            session = self.cal.session_of(sec)
            if session is None:
                continue
            bar = acc.bar(self.symbol, sec, session)
            if bar is not None:  # 快照看不到每笔成交：没有 vwap 和成交笔数
                bar = dataclasses.replace(bar, vwap=None, trades=None)
                out.append(BuiltBar(bar, now, frozenset({"degraded"})))
        self.sealed_until = max(self.sealed_until, cutoff + NS_PER_SEC)
        return out
