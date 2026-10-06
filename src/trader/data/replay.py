"""历史回放行情源，全速模式（DESIGN.md 5.4）：按天惰性加载，多标的按可用时间归并。

定速模式（replay 界面用）在阶段 5 实现。
"""

from __future__ import annotations

import heapq
from collections.abc import Iterator
from typing import Literal

from trader.core.events import BarEvent
from trader.core.models import Bar, MarketMeta
from trader.core.timeutil import NS_PER_SEC, ny_date
from trader.data.history import HistoryService, SessionFilter

FeedKind = Literal["bar_1s", "tick", "quote"]


class HistoricalBarFeed:
    """BacktestFeed：输出 1 秒 BarEvent。

    bar 的 available_time = ts_end + decision_delay_ns：策略最早在 bar 收盘后才能看到它。
    同一可用时间的多个标的按订阅顺序输出，保证确定性。
    """

    def __init__(
        self,
        history: HistoryService,
        session: SessionFilter = "rth",
        decision_delay_ns: int = 0,
    ) -> None:
        self._history = history
        self._session: SessionFilter = session
        self._delay = decision_delay_ns
        self._symbols: list[str] = []

    def subscribe(self, symbols: list[str], kinds: set[FeedKind]) -> None:
        if kinds - {"bar_1s"}:
            raise ValueError(f"历史回放目前只提供 bar_1s，不支持：{kinds - {'bar_1s'}}")
        for s in symbols:
            if s not in self._symbols:
                self._symbols.append(s)

    def unsubscribe(self, symbols: list[str]) -> None:
        self._symbols = [s for s in self._symbols if s not in symbols]

    def events(self, start: int, end: int) -> Iterator[BarEvent]:
        """输出 ts_start 在 [start, end) 内的 bar，按 (available_time, 订阅顺序) 排列。"""
        cal = self._history.cal
        seq = 0
        for day in cal.trading_days(ny_date(start), ny_date(end - 1)):
            lo, hi = max(start, day.pre_open), min(end, day.post_close)
            streams = [
                self._day_stream(rank, sym, lo, hi) for rank, sym in enumerate(self._symbols)
            ]
            for _, _, bar in heapq.merge(*streams):
                meta = MarketMeta(
                    source="massive",
                    feed_kind="agg_1s",
                    event_time=bar.ts_start,
                    received_time=None,
                    available_time=bar.ts_end + self._delay,
                    sequence=seq,
                )
                seq += 1
                yield BarEvent(bar, meta)

    def _day_stream(self, rank: int, symbol: str, lo: int, hi: int) -> Iterator[tuple[int, int, Bar]]:
        df = self._history.bars(symbol, "1s", lo, hi, session=self._session)
        for ts, o, h, low, c, v, vw, n in df.iter_rows():
            # 1 秒 bar 的 ts_end = ts_start + 1s，排序键用它代替可用时间（延迟对所有 bar 相同）
            yield ts + NS_PER_SEC, rank, Bar(symbol, "1s", ts, o, h, low, c, v, vw, n)
