"""美股交易日历：节假日、半日市、各交易时段的起止时间（基于 exchange_calendars 的 XNYS）。

时段名称与下载工具的 session 列一致：pre（盘前）、regular（常规）、post（盘后）。
盘前从 04:00 开始；盘后正常到 20:00，半日市到 17:00。时间都是纽约当地时间。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time, timedelta
from functools import cached_property
from typing import Literal

import exchange_calendars as xcals

from trader.core.events import SessionEvent, SessionKind
from trader.core.timeutil import ny_date, ny_to_ns, to_ns

Session = Literal["pre", "regular", "post"]

PRE_OPEN = time(4, 0)
POST_CLOSE = time(20, 0)
POST_CLOSE_EARLY = time(17, 0)  # 半日市的盘后结束时间


@dataclass(frozen=True, slots=True)
class TradingDay:
    """一个交易日各时段的边界，UTC 纳秒。区间都是左闭右开。"""

    day: date
    pre_open: int
    open: int
    close: int
    post_close: int
    early_close: bool

    def session_of(self, ts: int) -> Session | None:
        """ts 所在的时段；不在任何时段内返回 None。"""
        if self.pre_open <= ts < self.open:
            return "pre"
        if self.open <= ts < self.close:
            return "regular"
        if self.close <= ts < self.post_close:
            return "post"
        return None

    def session_events(self) -> list[SessionEvent]:
        return [
            SessionEvent(SessionKind.PRE_OPEN, self.pre_open),
            SessionEvent(SessionKind.OPEN, self.open),
            SessionEvent(SessionKind.CLOSE, self.close),
            SessionEvent(SessionKind.POST_CLOSE, self.post_close),
        ]


class TradingCalendar:
    """覆盖范围由 exchange_calendars 决定（约过去 20 年到未来 1 年），超出范围会报错。"""

    def __init__(self, exchange: str = "XNYS") -> None:
        self._cal = xcals.get_calendar(exchange)

    @cached_property
    def _early_closes(self) -> frozenset[date]:
        return frozenset(d.date() for d in self._cal.early_closes)

    def is_trading_day(self, day: date) -> bool:
        return bool(self._cal.is_session(day.isoformat()))

    def trading_day(self, day: date) -> TradingDay:
        if not self.is_trading_day(day):
            raise ValueError(f"{day} 不是交易日")
        label = day.isoformat()
        early = day in self._early_closes
        return TradingDay(
            day=day,
            pre_open=ny_to_ns(day, PRE_OPEN),
            open=to_ns(self._cal.session_open(label).to_pydatetime()),
            close=to_ns(self._cal.session_close(label).to_pydatetime()),
            post_close=ny_to_ns(day, POST_CLOSE_EARLY if early else POST_CLOSE),
            early_close=early,
        )

    def trading_days(self, start: date, end: date) -> list[TradingDay]:
        """[start, end] 两端都包含的全部交易日。"""
        labels = self._cal.sessions_in_range(start.isoformat(), end.isoformat())
        return [self.trading_day(d.date()) for d in labels]

    def next_trading_day(self, day: date) -> date:
        """day 之后（不含 day）的第一个交易日。"""
        d = day + timedelta(days=1)
        while not self.is_trading_day(d):
            d += timedelta(days=1)
        return d

    def previous_trading_day(self, day: date) -> date:
        """day 之前（不含 day）的最后一个交易日。"""
        d = day - timedelta(days=1)
        while not self.is_trading_day(d):
            d -= timedelta(days=1)
        return d

    def session_of(self, ts: int) -> Session | None:
        """ts 所在的时段；非交易日或不在任何时段内返回 None。"""
        day = ny_date(ts)
        if not self.is_trading_day(day):
            return None
        return self.trading_day(day).session_of(ts)
