"""IBKR 实时行情源（DESIGN.md 5.5）。

启动顺序（固定）：
1. 核对逐笔订阅数量不超过账户额度（超出直接报错，不自动改用快照）；
2. 订阅实时数据，生成器从“交接边界”H = 订阅时刻所在秒的下一秒开始收数据；
3. 等几秒后向 IB 请求补数：当天的 1 分钟 bar（整天）和最近 30 分钟的 1 秒 bar，只取 H 之前的部分；
4. 补数装进 LiveHistory，之后引擎订阅、指标预热都从这里取；
   补不上的标的发 SystemEvent（策略保持暂停）。
之后由驱动方循环调用 poll(now)：封口到期的 1 秒 bar，返回 BarEvent 和看门狗的 SystemEvent。

重连后 resync()：重新订阅，用 1 秒 bar 补上断线期间的缺口（超过 30 分钟的部分记为缺口）。
实时 tick、生成的 1 秒 bar 和迟到修订都落盘到 data/live/<日期>/，用于事后与 Massive 对比。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Literal

import polars as pl

from trader.brokers.ibkr.api import HistBar, IbApi, Snapshot
from trader.core.events import BarEvent, SystemEvent, SystemKind
from trader.core.models import Bar, MarketMeta
from trader.core.timeutil import NS_PER_MIN, NS_PER_SEC, from_ns
from trader.core.trading_calendar import TradingCalendar
from trader.data.bar_builder import (
    DEFAULT_LIVE_GRACE_MS,
    BuiltBar,
    SnapshotBarBuilder,
    TickBarBuilder,
    TradeTick,
)
from trader.data.live_history import LiveHistory
from trader.data.store import BAR_SCHEMA


@dataclass(frozen=True, slots=True)
class FeedCapabilities:
    trades_complete: bool  # 是否完整逐笔成交
    quotes_available: bool
    volume_semantics: Literal["per_trade", "cumulative_snapshot", "aggregated"]
    timestamp_precision_ns: int
    min_bar_interval: str  # 能可靠生成的最小 bar 周期


TICK_CAPS = FeedCapabilities(True, False, "per_trade", NS_PER_SEC, "1s")
SNAPSHOT_CAPS = FeedCapabilities(False, True, "cumulative_snapshot", NS_PER_SEC, "1m")


class FeedConfigError(ValueError):
    pass


@dataclass
class FeedSettings:
    tick_symbols: list[str]
    snapshot_symbols: list[str]
    tick_limit: int = 5  # 账户的逐笔并发额度
    grace_ms: int = DEFAULT_LIVE_GRACE_MS
    watchdog_regular_s: int = 30  # 常规时段内超过这么久没有任何更新就报警
    watchdog_extended_s: int = 600  # 盘前盘后成交稀疏，阈值放宽
    fine_backfill_s: int = 1800  # 1 秒 bar 补数的时长（IB 单次请求上限 2000 秒）
    settle_s: float = 3.0  # 订阅后等多久再请求补数，保证边界之前的秒已在 IB 历史数据里
    request_gap_s: float = 0.3  # 补数请求之间的间隔（IB 限速：10 分钟 60 次）

    def __post_init__(self) -> None:
        both = set(self.tick_symbols) & set(self.snapshot_symbols)
        if both:
            raise FeedConfigError(f"标的不能同时用逐笔和快照：{sorted(both)}")
        if len(self.tick_symbols) > self.tick_limit:
            raise FeedConfigError(
                f"逐笔订阅 {len(self.tick_symbols)} 个，超过账户额度 {self.tick_limit} 个："
                f"{self.tick_symbols}。请在配置里把多出的标的改为快照（不会自动降级）"
            )


@dataclass
class _SymbolState:
    kind: Literal["tick", "snapshot"]
    builder: TickBarBuilder | SnapshotBarBuilder
    subscribed_at: int = 0
    stale: bool = False
    gap: bool = False


@dataclass
class IbkrFeed:
    api: IbApi
    cal: TradingCalendar
    history: LiveHistory
    settings: FeedSettings
    now: Callable[[], int]
    record_dir: Path | None = None
    symbols: dict[str, _SymbolState] = field(default_factory=dict)
    _seq: int = 0
    _system: list[SystemEvent] = field(default_factory=list)
    _files: dict[str, IO[str]] = field(default_factory=dict)
    connected: bool = False
    revisions: int = 0

    # ---------- 能力 ----------

    def capabilities(self, symbol: str) -> FeedCapabilities:
        st = self.symbols.get(symbol)
        return TICK_CAPS if st is not None and st.kind == "tick" else SNAPSHOT_CAPS

    # ---------- 启动 ----------

    async def start(self) -> list[SystemEvent]:
        """订阅 → 补数 → 交接。返回启动过程中的问题（缺口、无法识别的代码）。"""
        self.api.on_error(self._on_error)
        self.api.on_disconnect(self._on_disconnect)
        self.connected = True
        wanted = self.settings.tick_symbols + self.settings.snapshot_symbols
        ok = await self.api.qualify(wanted)
        for s in wanted:
            if s not in ok:
                self._sys(SystemKind.FEED_INTERRUPTED, f"IB 无法识别代码 {s}，未订阅", s)
        todo = [s for s in wanted if s in ok]
        for s in todo:
            self._subscribe(s)
        await asyncio.sleep(self.settings.settle_s)
        for s in todo:
            await self._backfill(s)
        return self.drain_system()

    async def add_symbol(self, symbol: str) -> None:
        """运行中新增标的（策略或界面订阅了清单里的新标的）：默认用快照。"""
        if symbol in self.symbols:
            return
        if symbol not in self.settings.tick_symbols:
            self.settings.snapshot_symbols.append(symbol)
        if not await self.api.qualify([symbol]):
            self._sys(SystemKind.FEED_INTERRUPTED, f"IB 无法识别代码 {symbol}", symbol)
            return
        self._subscribe(symbol)
        await asyncio.sleep(self.settings.settle_s)
        await self._backfill(symbol)

    def _subscribe(self, symbol: str) -> None:
        now = self.now()
        boundary = (now // NS_PER_SEC + 1) * NS_PER_SEC
        grace = self.settings.grace_ms * 10**6
        if symbol in self.settings.tick_symbols:
            b: TickBarBuilder | SnapshotBarBuilder = TickBarBuilder(
                symbol, self.cal, grace, start_ns=boundary
            )
            st = _SymbolState("tick", b, now)
            self.symbols[symbol] = st
            self.api.subscribe_ticks(symbol, self._on_trade)
        else:
            b = SnapshotBarBuilder(symbol, self.cal, grace, start_ns=boundary)
            st = _SymbolState("snapshot", b, now)
            self.symbols[symbol] = st
            self.api.subscribe_snapshots(symbol, self._on_snapshot)

    async def _backfill(self, symbol: str) -> None:
        st = self.symbols[symbol]
        boundary = st.builder.start_ns
        loc = self.cal.locate(boundary)
        day = loc[0] if loc is not None else None
        if day is None:  # 不在交易时段内启动：当天没有可补的数据
            nxt = self.cal.trading_days(from_ns(boundary).date(), from_ns(boundary).date())
            day = nxt[0] if nxt else None
            if day is None or boundary < day.start:
                return
        fine: list[HistBar] = []
        coarse: list[HistBar] = []
        try:
            fine = await self.api.historical_bars(
                symbol, f"{self.settings.fine_backfill_s} S", "1 secs"
            )
            await asyncio.sleep(self.settings.request_gap_s)
            coarse = await self.api.historical_bars(symbol, "1 D", "1 min")
            await asyncio.sleep(self.settings.request_gap_s)
        except Exception as exc:  # noqa: BLE001 - IB 的各种错误都按补数失败处理
            st.gap = True
            self._sys(SystemKind.FEED_INTERRUPTED, f"补数失败：{exc}，当天数据有缺口", symbol)
        fine = [b for b in fine if day.start <= b.ts < boundary]
        # 1 秒数据从 fine_from（整分钟）开始用；之前用 1 分钟 bar。1 秒补数为空或开始得晚
        # （成交稀疏）时，1 分钟 bar 一直用到 1 秒数据开始的那一分钟，中间不留空。
        # 交接边界所在的那一分钟不能用 1 分钟 bar（它和实时数据重叠）。
        window_start = boundary - self.settings.fine_backfill_s * NS_PER_SEC
        first = fine[0].ts if fine else boundary
        fine_from = -(-max(window_start, first) // NS_PER_MIN) * NS_PER_MIN
        fine_from = max(day.start, min(fine_from, boundary // NS_PER_MIN * NS_PER_MIN))
        coarse = [b for b in coarse if day.start <= b.ts < fine_from]
        self.history.set_backfill(symbol, _frame(coarse), _frame(fine), fine_from)
        if not st.gap and boundary - day.start > 10 * NS_PER_MIN and not coarse and not fine:
            st.gap = True
            self._sys(SystemKind.FEED_INTERRUPTED, "当天补数为空，数据可能有缺口", symbol)

    # ---------- 实时回调 ----------

    def _on_trade(self, t: TradeTick) -> None:
        st = self.symbols.get(t.symbol)
        if st is None:
            return
        st.builder.add(t)  # type: ignore[arg-type]
        self._write(
            "tbt",
            {
                "sym": t.symbol, "recv": t.recv, "ts": t.ts, "price": t.price, "size": t.size,
                "exch": t.exchange, "cond": t.conditions, "unreported": t.unreported,
            },
        )  # fmt: skip

    def _on_snapshot(self, s: Snapshot) -> None:
        st = self.symbols.get(s.symbol)
        if st is None or not isinstance(st.builder, SnapshotBarBuilder):
            return
        st.builder.add(s.recv, s.last, s.volume)
        st.builder.last_recv = max(st.builder.last_recv, s.recv)
        self._write(
            "mkt",
            {
                "sym": s.symbol, "recv": s.recv, "last": s.last, "volume": s.volume,
                "bid": s.bid, "ask": s.ask,
            },
        )  # fmt: skip

    def _on_error(self, code: int, msg: str, symbol: str | None) -> None:
        if code in (2104, 2106, 2107, 2108, 2158, 2119):  # 行情农场连接状态的通知
            return
        if code == 1100:
            self.connected = False
            self._sys(SystemKind.CONNECTION_LOST, f"IB {code}: {msg}")
        elif code in (1101, 1102):
            self.connected = True
            self._sys(SystemKind.CONNECTION_RESTORED, f"IB {code}: {msg}")
        elif code == 10190 or code in (354, 10089, 10167, 10168):  # 超过逐笔额度
            self._sys(SystemKind.FEED_DEGRADED, f"IB {code}: {msg}", symbol)
        elif code in (10189, 10197):  # 实盘账户在别处登录、行情共享冲突
            self._sys(SystemKind.FEED_INTERRUPTED, f"IB {code}: {msg}", symbol)

    def _on_disconnect(self) -> None:
        self.connected = False
        self._sys(SystemKind.CONNECTION_LOST, "与 IB Gateway 的连接断开")

    # ---------- 轮询 ----------

    def poll(self, now: int) -> list[BarEvent]:
        """封口到期的 1 秒 bar（按可用时间排序）；同时运行看门狗。"""
        out: list[BarEvent] = []
        grace = self.settings.grace_ms * 10**6
        for sym, st in self.symbols.items():
            b = st.builder
            for built in b.seal(now):
                out.append(self._event(built, st.kind, grace))
                self.history.append(built.bar)
                self._write("bars", _bar_record(built))
            if isinstance(b, TickBarBuilder) and b.revisions:
                self.revisions += len(b.revisions)
                for r in b.revisions:
                    self._write("revisions", _bar_record(r))
                b.revisions.clear()
            self._watchdog(sym, st, now)
        out.sort(key=lambda e: (e.available_time, e.meta.sequence))
        return out

    def _event(self, built: BuiltBar, kind: str, grace: int) -> BarEvent:
        self._seq += 1
        bar = built.bar
        meta = MarketMeta(
            source="ibkr",
            feed_kind="tick_by_tick" if kind == "tick" else "snapshot",
            event_time=bar.ts_start,
            received_time=built.sealed_at,
            available_time=bar.ts_end + grace,
            sequence=self._seq,
            quality_flags=built.flags,
        )
        return BarEvent(bar, meta)

    def _watchdog(self, sym: str, st: _SymbolState, now: int) -> None:
        session = self.cal.session_of(now)
        if session is None or not self.connected:
            return
        limit = (
            self.settings.watchdog_regular_s
            if session == "regular"
            else self.settings.watchdog_extended_s
        )
        last = max(st.builder.last_recv, st.subscribed_at)
        if not st.stale and now - last > limit * NS_PER_SEC:
            st.stale = True
            self._sys(SystemKind.FEED_INTERRUPTED, f"{limit} 秒没有收到任何行情更新", sym, ts=now)
        elif st.stale and now - last <= limit * NS_PER_SEC:
            st.stale = False
            self._sys(SystemKind.FEED_RESTORED, "行情恢复", sym, ts=now)

    # ---------- 重连 ----------

    async def resync(self) -> None:
        """重连后：重新订阅全部标的，用 1 秒 bar 补上断线期间封口不到的部分。"""
        self.connected = True
        old = {s: st.builder.sealed_until for s, st in self.symbols.items()}
        for s in list(self.symbols):
            self.api.unsubscribe(s)
            self._subscribe(s)
        await asyncio.sleep(self.settings.settle_s)
        for s, since in old.items():
            boundary = self.symbols[s].builder.start_ns
            if since <= 0 or boundary <= since:
                continue
            if boundary - since > self.settings.fine_backfill_s * NS_PER_SEC:
                self.symbols[s].gap = True
                self._sys(
                    SystemKind.FEED_INTERRUPTED,
                    f"断线 {(boundary - since) // NS_PER_SEC} 秒，超过可补范围，记为缺口",
                    s,
                )
                continue
            try:
                bars = await self.api.historical_bars(
                    s, f"{self.settings.fine_backfill_s} S", "1 secs"
                )
            except Exception as exc:  # noqa: BLE001
                self.symbols[s].gap = True
                self._sys(SystemKind.FEED_INTERRUPTED, f"断线补数失败：{exc}", s)
                continue
            for hb in bars:
                if since <= hb.ts < boundary and self.cal.session_of(hb.ts) is not None:
                    self.history.append(_hist_to_bar(s, hb, self.cal.session_of(hb.ts)))
            await asyncio.sleep(self.settings.request_gap_s)

    # ---------- 系统事件与落盘 ----------

    def _sys(
        self, kind: SystemKind, detail: str, symbol: str | None = None, ts: int | None = None
    ) -> None:
        self._system.append(SystemEvent(kind, self.now() if ts is None else ts, detail, symbol))

    def drain_system(self) -> list[SystemEvent]:
        out, self._system = self._system, []
        return out

    def _write(self, name: str, rec: dict) -> None:
        if self.record_dir is None:
            return
        fh = self._files.get(name)
        if fh is None:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            fh = (self.record_dir / f"{name}.jsonl").open("a", encoding="utf-8")
            self._files[name] = fh
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def flush(self) -> None:
        for fh in self._files.values():
            fh.flush()

    def close(self) -> None:
        for fh in self._files.values():
            fh.close()
        self._files.clear()


def _frame(bars: list[HistBar]) -> pl.DataFrame:
    rows = [(b.ts, b.open, b.high, b.low, b.close, b.volume, b.vwap, b.trades) for b in bars]
    return pl.DataFrame(rows, schema=BAR_SCHEMA, orient="row")


def _hist_to_bar(symbol: str, b: HistBar, session: str | None) -> Bar:
    return Bar(
        symbol, "1s", b.ts, b.open, b.high, b.low, b.close, b.volume, b.vwap, b.trades,
        session, b.ts + NS_PER_SEC,
    )  # fmt: skip


def _bar_record(b: BuiltBar) -> dict:
    x = b.bar
    return {
        "sym": x.symbol, "ts": x.ts_start, "o": x.open, "h": x.high, "l": x.low, "c": x.close,
        "v": x.volume, "n": x.trades, "sealed": b.sealed_at, "flags": sorted(b.flags),
    }  # fmt: skip
