"""IBKR 实时行情源（用假的 IB 客户端）：额度检查、启动补数与交接、去重、看门狗、断线重连。"""

from __future__ import annotations

import asyncio
from datetime import date, time
from pathlib import Path

import pytest
from tests.fake_ib import FakeIb

from trader.brokers.ibkr.api import HistBar, SafetyError, Snapshot, check_paper_accounts
from trader.brokers.ibkr.feed import FeedConfigError, FeedSettings, IbkrFeed
from trader.core.events import SystemKind
from trader.core.timeutil import NS_PER_MIN, NS_PER_SEC, ny_to_ns
from trader.core.trading_calendar import TradingCalendar
from trader.data.bar_builder import TradeTick
from trader.data.catalog import Catalog
from trader.data.live_history import LiveHistory

CAL = TradingCalendar()
DAY = date(2026, 10, 6)
TD = CAL.trading_day(DAY)
S = NS_PER_SEC
T_START = ny_to_ns(DAY, time(10, 30)) + 400 * 10**6  # 10:30:00.4 启动
BOUNDARY = ny_to_ns(DAY, time(10, 30, 1))


class Clock:
    def __init__(self, t: int) -> None:
        self.t = t

    def __call__(self) -> int:
        return self.t


def hb(ts: int, px: float, v: float = 100) -> HistBar:
    return HistBar(ts, px, px, px, px, v, px, 1)


def make(tmp_path: Path, tick=("SPY",), snap=("NVDA",)):  # noqa: ANN001, ANN201
    ib = FakeIb()
    clock = Clock(T_START)
    ib.clock = clock
    for sym in (*tick, *snap):  # 默认有当天上午的 1 分钟补数
        ib.hist[(sym, "1 min")] = [hb(TD.start + i * NS_PER_MIN, 100.0) for i in range(300)]
    hist = LiveHistory(tmp_path, CAL, Catalog(tmp_path / "catalog.sqlite"), DAY)
    settings = FeedSettings(list(tick), list(snap), settle_s=0, request_gap_s=0)
    feed = IbkrFeed(ib, CAL, hist, settings, clock, record_dir=tmp_path / "live")
    return ib, clock, hist, feed


def test_tick_limit_is_enforced_not_degraded():
    with pytest.raises(FeedConfigError, match="超过账户额度"):
        FeedSettings(["A", "B", "C", "D", "E", "F"], [])
    with pytest.raises(FeedConfigError, match="不能同时"):
        FeedSettings(["A"], ["A"])


def test_paper_safety_checks():
    check_paper_accounts(["DU1234567"])
    with pytest.raises(SafetyError):
        check_paper_accounts(["U7654321"])
    with pytest.raises(SafetyError):
        check_paper_accounts([])
    with pytest.raises(SafetyError, match="4001"):
        asyncio.run(FakeIb().connect("127.0.0.1", 4001, 1, True))


def test_startup_backfill_and_handoff_without_duplicates(tmp_path: Path):
    ib, clock, hist, feed = make(tmp_path)
    # IB 补数：1 分钟 bar 覆盖到 10:30（10:30 这一分钟越过交接边界，必须被丢弃）
    ib.hist[("SPY", "1 min")] = [hb(TD.start + i * NS_PER_MIN, 100 + i * 0.01) for i in range(391)]
    # 1 秒 bar：最近 30 分钟，最后几秒越过了交接边界（必须被丢弃）
    fine_lo = BOUNDARY - 1800 * S
    ib.hist[("SPY", "1 secs")] = [hb(fine_lo + i * S, 200.0, 10) for i in range(1806)]
    events = asyncio.run(feed.start())
    assert events == []
    assert hist.fine_from["SPY"] == ny_to_ns(DAY, time(10, 1))  # 30 分钟窗口按整分钟取
    # 实时成交：边界前一秒（属于补数）丢弃，边界及之后收下
    ib.trade(TradeTick("SPY", BOUNDARY - S, 999.0, 1, clock.t))
    ib.trade(TradeTick("SPY", BOUNDARY, 300.0, 5, clock.t + S))
    ib.trade(TradeTick("SPY", BOUNDARY + S, 301.0, 5, clock.t + 2 * S))
    clock.t = BOUNDARY + 3 * S
    evs = feed.poll(clock.t)
    assert [e.bar.ts_start for e in evs] == [BOUNDARY, BOUNDARY + S]
    assert all(e.available_time == e.bar.ts_end + 1000 * 10**6 for e in evs)
    assert evs[0].meta.feed_kind == "tick_by_tick" and not evs[0].meta.quality_flags
    # 当天 1 秒数据：补数 [10:01, 边界) + 实时，没有重复
    secs = hist.day_bars("SPY", "1s", TD, "extended")
    ts = secs["ts_start"].to_list()
    assert len(ts) == len(set(ts)) and ts == sorted(ts)
    assert ts[0] == ny_to_ns(DAY, time(10, 1)) and ts[-1] == BOUNDARY + S
    assert 999.0 not in secs["high"].to_list()
    # 1 分钟：10:01 之前来自 1 分钟补数，之后来自 1 秒；10:30 这一分钟 = 补数 1 秒 + 实时
    mins = hist.day_bars("SPY", "1m", TD, "extended")
    mts = mins["ts_start"].to_list()
    assert len(mts) == len(set(mts))
    assert mts[0] == TD.start and mts[-1] == ny_to_ns(DAY, time(10, 30))
    assert mins.filter(mins["ts_start"] < ny_to_ns(DAY, time(10, 1)))[
        "volume"
    ].unique().to_list() == [100]
    last = mins.row(-1, named=True)
    assert (last["open"], last["close"], last["volume"]) == (200.0, 301.0, 10 + 5 + 5)
    feed.flush()
    assert (tmp_path / "live" / "tbt.jsonl").read_text(encoding="utf-8").count("\n") == 3
    assert (tmp_path / "live" / "bars.jsonl").read_text(encoding="utf-8").count("\n") == 2


def test_snapshot_symbols_are_degraded(tmp_path: Path):
    ib, clock, hist, feed = make(tmp_path)
    asyncio.run(feed.start())
    assert feed.capabilities("SPY").trades_complete
    assert not feed.capabilities("NVDA").trades_complete
    ib.snap(Snapshot("NVDA", BOUNDARY + 100, 180.0, 1_000_000))
    ib.snap(Snapshot("NVDA", BOUNDARY + 200, 180.5, 1_000_300))
    evs = feed.poll(BOUNDARY + 3 * S)
    assert len(evs) == 1 and "degraded" in evs[0].meta.quality_flags
    assert evs[0].meta.feed_kind == "snapshot"


def test_backfill_failure_reports_gap(tmp_path: Path):
    ib, clock, hist, feed = make(tmp_path)
    ib.hist_fail.add("NVDA")
    events = asyncio.run(feed.start())
    kinds = {(e.kind, e.symbol) for e in events}
    assert (SystemKind.FEED_INTERRUPTED, "NVDA") in kinds
    assert feed.symbols["NVDA"].gap and not feed.symbols["SPY"].gap


def test_unknown_symbol_reported(tmp_path: Path):
    ib, clock, hist, feed = make(tmp_path, snap=("NVDA", "ZZZZ"))
    ib.known = {"SPY", "NVDA"}
    events = asyncio.run(feed.start())
    assert any(e.symbol == "ZZZZ" for e in events)
    assert "ZZZZ" not in feed.symbols


def test_watchdog_interrupt_and_restore(tmp_path: Path):
    ib, clock, hist, feed = make(tmp_path, snap=())
    asyncio.run(feed.start())
    feed.poll(T_START + 20 * S)
    assert feed.drain_system() == []
    feed.poll(T_START + 31 * S)
    ev = feed.drain_system()
    assert [(e.kind, e.symbol) for e in ev] == [(SystemKind.FEED_INTERRUPTED, "SPY")]
    ib.trade(TradeTick("SPY", T_START + 32 * S, 300.0, 1, T_START + 32 * S))
    feed.poll(T_START + 33 * S)
    assert [e.kind for e in feed.drain_system()] == [SystemKind.FEED_RESTORED]


def test_connection_errors_and_resync_fill_gap(tmp_path: Path):
    ib, clock, hist, feed = make(tmp_path, snap=())
    asyncio.run(feed.start())
    ib.trade(TradeTick("SPY", BOUNDARY, 300.0, 5, BOUNDARY + 500))
    feed.poll(BOUNDARY + 3 * S)
    sealed = feed.symbols["SPY"].builder.sealed_until
    ib.error(2104, "Market data farm connection is OK")  # 普通通知：忽略
    ib.drop()
    ev = feed.drain_system()
    assert [e.kind for e in ev] == [SystemKind.CONNECTION_LOST] and not feed.connected
    # 断线 2 分钟后重连：用 1 秒 bar 补上 [已封口, 新边界)
    clock.t = BOUNDARY + 120 * S + 300 * 10**6
    ib.hist[("SPY", "1 secs")] = [hb(sealed + i * S, 301.0, 7) for i in range(0, 200, 10)]
    asyncio.run(feed.resync())
    new_boundary = feed.symbols["SPY"].builder.start_ns
    assert new_boundary == BOUNDARY + 121 * S and feed.connected
    secs = hist.day_bars("SPY", "1s", TD, "extended")["ts_start"].to_list()
    assert len(secs) == len(set(secs))
    gap_fill = [t for t in secs if sealed <= t < new_boundary]
    assert len(gap_fill) == 12  # 0,10,...,110 秒
    assert "SPY" in ib.tick_subs  # 已重新订阅


def test_long_disconnect_is_a_gap(tmp_path: Path):
    ib, clock, hist, feed = make(tmp_path, snap=())
    asyncio.run(feed.start())
    ib.trade(TradeTick("SPY", BOUNDARY, 300.0, 5, BOUNDARY + 500))
    feed.poll(BOUNDARY + 3 * S)
    clock.t = BOUNDARY + 3600 * S
    asyncio.run(feed.resync())
    ev = feed.drain_system()
    assert any(e.kind == SystemKind.FEED_INTERRUPTED and "缺口" in e.detail for e in ev)
    assert feed.symbols["SPY"].gap


def test_history_service_timeouts_stop_waiting(tmp_path: Path):
    """IB 历史数据服务无响应（例如凌晨 4 点刚开盘）：
    连续两次超时后其余标的不再请求，启动不被拖住。"""
    ib, clock, hist, feed = make(tmp_path, tick=("SPY", "QQQ"), snap=("NVDA", "AMD", "TSLA"))

    async def slow(symbol: str, duration: str, bar_size: str, end: int | None = None):  # noqa: ANN202
        ib.hist_calls.append((symbol, duration, bar_size))
        raise TimeoutError

    ib.historical_bars = slow  # type: ignore[method-assign]
    events = asyncio.run(feed.start())
    assert len(ib.hist_calls) == 2  # 只等了两次
    assert all(st.gap for st in feed.symbols.values())
    details = [e.detail for e in events]
    assert any("连续超时" in d for d in details)
    assert any("跳过补数" in d and "AMD" in d for d in details)


def test_backfill_duration_follows_elapsed_time(tmp_path: Path):
    """刚开盘 5 分钟启动：1 秒补数只请求 6 分钟，不请求整天的 1 分钟 bar。"""
    ib, clock, hist, feed = make(tmp_path, snap=())
    clock.t = TD.start + 5 * NS_PER_MIN + 300 * 10**6
    asyncio.run(feed.start())
    assert ib.hist_calls == [("SPY", "361 S", "1 secs")]
