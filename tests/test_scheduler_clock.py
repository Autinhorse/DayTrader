import asyncio

import pytest

from trader.core.clock import SimClock, WallClock
from trader.core.events import (
    BarEvent,
    FillEvent,
    Phase,
    SessionEvent,
    SessionKind,
    SystemEvent,
    SystemKind,
    TimerEvent,
)
from trader.core.models import Bar, Fill, MarketMeta
from trader.core.scheduler import EventScheduler
from trader.core.timeutil import NS_PER_SEC

T = 1_000 * NS_PER_SEC


def bar_event(ts_start: int, symbol: str = "AAPL") -> BarEvent:
    bar = Bar(symbol, "1s", ts_start, 1, 1, 1, 1, 100)
    meta = MarketMeta("synthetic", "agg_1s", ts_start, None, bar.ts_end, 0)
    return BarEvent(bar, meta)


def drain(s: EventScheduler) -> list:
    out = []
    while (e := s.pop()) is not None:
        out.append(e)
    return out


def test_order_by_time_then_phase_then_seq():
    s = EventScheduler()
    timer = TimerEvent("t", T)
    system = SystemEvent(SystemKind.FEED_DEGRADED, T)
    session = SessionEvent(SessionKind.OPEN, T)
    bar = bar_event(T - NS_PER_SEC)  # available_time = T
    fill = FillEvent(Fill("o1", T, 1, 1, "e1"))  # type: ignore[arg-type]
    later_bar = bar_event(T)  # available_time = T + 1s
    for e in (later_bar, timer, system, session, bar, fill):
        s.push(e)
    assert drain(s) == [fill, bar, session, timer, system, later_bar]


def test_same_phase_is_fifo():
    s = EventScheduler()
    a, b, c = (bar_event(T, sym) for sym in ("A", "B", "C"))
    for e in (b, c, a):
        s.push(e)
    assert drain(s) == [b, c, a]


def test_explicit_phase_override():
    s = EventScheduler()
    bar = bar_event(T - NS_PER_SEC)
    match_marker = TimerEvent("match", T)
    s.push(bar)
    s.push(match_marker, phase=Phase.MATCH)
    assert drain(s) == [match_marker, bar]


def test_peek_and_len():
    s = EventScheduler()
    assert s.pop() is None and s.peek_time() is None and len(s) == 0
    s.push(TimerEvent("x", T))
    assert s.peek_time() == T and len(s) == 1


def test_sim_clock_timer_fires_during_sparse_data():
    """行情稀疏时定时器照常触发：两根 bar 之间相隔一小时，中间的定时器按时出队。"""
    s = EventScheduler()
    clock = SimClock(s, start=0)
    s.push(bar_event(T))
    s.push(bar_event(T + 3600 * NS_PER_SEC))
    clock.set_timer("half_hour", T + 1800 * NS_PER_SEC)
    seen = []
    while (e := s.pop()) is not None:
        clock.advance_to(e.available_time)
        seen.append((type(e).__name__, clock.now()))
    assert [name for name, _ in seen] == ["BarEvent", "TimerEvent", "BarEvent"]
    assert seen[1][1] == T + 1800 * NS_PER_SEC


def test_sim_clock_rejects_going_back():
    clock = SimClock(EventScheduler(), start=T)
    with pytest.raises(ValueError):
        clock.advance_to(T - 1)
    with pytest.raises(ValueError):
        clock.set_timer("past", T - 1)
    clock.set_timer("now", T)  # 当前时刻允许


def test_wall_clock_timer():
    async def run() -> list[TimerEvent]:
        fired: list[TimerEvent] = []
        clock = WallClock(fired.append)
        clock.set_timer("soon", clock.now() + 10_000_000)  # 10 毫秒后
        clock.set_timer("past", 0)  # 已过时间立即触发
        await asyncio.sleep(0.1)
        return fired

    fired = asyncio.run(run())
    assert [e.name for e in fired] == ["past", "soon"]
