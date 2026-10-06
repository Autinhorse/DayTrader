"""时钟（DESIGN.md 4.4）。策略和核心模块只能通过 Clock 取时间。

本文件是全项目唯一允许读取系统时间的地方，由 tests/test_architecture.py 强制。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Protocol

from trader.core.events import TimerEvent
from trader.core.scheduler import EventScheduler


class Clock(Protocol):
    def now(self) -> int: ...  # UTC 纳秒

    def set_timer(self, name: str, at: int) -> None: ...


class SimClock:
    """模拟时钟：时间只由引擎在处理事件时推进，定时器作为事件进入同一个调度器。

    因为定时器和行情在同一个调度器里排序，行情稀疏时定时器照常按时触发。
    """

    def __init__(self, scheduler: EventScheduler, start: int) -> None:
        self._scheduler = scheduler
        self._now = start

    def now(self) -> int:
        return self._now

    def advance_to(self, ts: int) -> None:
        """引擎在处理每个事件前调用。时间不能倒退。"""
        if ts < self._now:
            raise ValueError(f"模拟时钟不能倒退：{ts} < {self._now}")
        self._now = ts

    def set_timer(self, name: str, at: int) -> None:
        if at < self._now:
            raise ValueError(f"定时器 {name!r} 的触发时间早于当前时间")
        self._scheduler.push(TimerEvent(name=name, at=at))


class WallClock:
    """真实时钟：取系统时间，定时器到点后把 TimerEvent 交给 sink（实时引擎的事件队列）。

    set_timer 必须在运行中的 asyncio 事件循环里调用。
    """

    def __init__(self, sink: Callable[[TimerEvent], None]) -> None:
        self._sink = sink

    def now(self) -> int:
        return time.time_ns()

    def set_timer(self, name: str, at: int) -> None:
        delay = max(0, at - self.now()) / 1e9
        asyncio.get_running_loop().call_later(delay, self._sink, TimerEvent(name=name, at=at))
