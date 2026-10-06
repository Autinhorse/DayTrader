"""确定性调度器（DESIGN.md 2.3）。

行情、订单回报、定时器和交易时段事件全部进入同一个调度器，按
(available_time, phase, 序号) 出队。序号是入队顺序，所以同一时刻、同一阶段的
事件先进先出；同样的入队顺序一定得到同样的出队顺序。
"""

from __future__ import annotations

import heapq
from itertools import count

from trader.core.events import Event, Phase


class EventScheduler:
    def __init__(self) -> None:
        self._heap: list[tuple[int, Phase, int, Event]] = []
        self._seq = count()

    def push(self, event: Event, phase: Phase | None = None) -> None:
        """入队。phase 默认取事件自身的阶段；撮合等引擎内部步骤可显式指定。"""
        p = event.phase if phase is None else phase
        heapq.heappush(self._heap, (event.available_time, p, next(self._seq), event))

    def pop(self) -> Event | None:
        """取出最早的事件；队列为空时返回 None。"""
        if not self._heap:
            return None
        return heapq.heappop(self._heap)[3]

    def peek_time(self) -> int | None:
        """最早事件的 available_time；队列为空时返回 None。"""
        return self._heap[0][0] if self._heap else None

    def __len__(self) -> int:
        return len(self._heap)
