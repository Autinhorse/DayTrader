"""美股交易日历：节假日、半日市、各交易时段的起止时间（基于 exchange_calendars 的 XNYS）。

时段（纽约时间，区间左闭右开）：
    overnight  夜盘 前一天 21:00–04:00（23/5 交易开始后才有，见 docs/decisions/0002）
    pre        盘前 04:00–09:30
    regular    常规 09:30–16:00（半日市到 13:00）
    post       盘后 16:00–20:00（半日市到 17:00）

交易日期按期货惯例：前一天 21:00 到当天 20:00 属于同一个交易日。没有夜盘时交易日期就是纽约日历日期。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time, timedelta
from functools import cached_property
from typing import Literal

import exchange_calendars as xcals

from trader.core.events import SessionEvent, SessionKind
from trader.core.timeutil import ny_date, ny_to_ns, to_ns

SessionName = Literal["overnight", "pre", "regular", "post"]
SessionFilter = Literal["rth", "extended"]  # rth 只含常规时段；extended 含全部时段

OVERNIGHT_OPEN = time(21, 0)  # 前一天
PRE_OPEN = time(4, 0)
POST_CLOSE = time(20, 0)
POST_CLOSE_EARLY = time(17, 0)  # 半日市的盘后结束时间


@dataclass(frozen=True, slots=True)
class Session:
    name: SessionName
    start: int  # UTC 纳秒
    end: int

    def contains(self, ts: int) -> bool:
        return self.start <= ts < self.end


@dataclass(frozen=True, slots=True)
class TradingDay:
    """一个交易日的全部时段，按时间顺序排列。"""

    day: date  # 交易日期
    sessions: tuple[Session, ...]
    early_close: bool

    def _get(self, name: SessionName) -> Session:
        for s in self.sessions:
            if s.name == name:
                return s
        raise KeyError(name)

    @property
    def start(self) -> int:
        """交易日第一个时段的开始（有夜盘时是前一天 21:00，否则是盘前 04:00）。"""
        return self.sessions[0].start

    @property
    def end(self) -> int:
        """交易日最后一个时段的结束（盘后结束）。"""
        return self.sessions[-1].end

    @property
    def pre_open(self) -> int:
        return self._get("pre").start

    @property
    def open(self) -> int:
        return self._get("regular").start

    @property
    def close(self) -> int:
        return self._get("regular").end

    @property
    def post_close(self) -> int:
        return self._get("post").end

    @property
    def has_overnight(self) -> bool:
        return self.sessions[0].name == "overnight"

    def included(self, sessions: SessionFilter) -> tuple[Session, ...]:
        return tuple(s for s in self.sessions if sessions == "extended" or s.name == "regular")

    def session_at(self, ts: int) -> Session | None:
        for s in self.sessions:
            if s.contains(ts):
                return s
        return None

    def session_of(self, ts: int) -> SessionName | None:
        """ts 所在的时段名；不在任何时段内返回 None。"""
        s = self.session_at(ts)
        return s.name if s else None

    def session_events(self) -> list[SessionEvent]:
        events = []
        if self.has_overnight:
            events.append(SessionEvent(SessionKind.OVERNIGHT_OPEN, self.start))
        events += [
            SessionEvent(SessionKind.PRE_OPEN, self.pre_open),
            SessionEvent(SessionKind.OPEN, self.open),
            SessionEvent(SessionKind.CLOSE, self.close),
            SessionEvent(SessionKind.POST_CLOSE, self.post_close),
        ]
        return events


class TradingCalendar:
    """覆盖范围由 exchange_calendars 决定（约过去 20 年到未来 1 年），超出范围会报错。

    overnight_from：从这个交易日起有夜盘。None 表示没有夜盘。23/5 的开始日期以
    SIP 就绪为前提，确认后再在配置里设置，见 docs/decisions/0002。
    """

    def __init__(self, exchange: str = "XNYS", overnight_from: date | None = None) -> None:
        self._cal = xcals.get_calendar(exchange)
        self.overnight_from = overnight_from
        self._days: dict[date, TradingDay] = {}

    @cached_property
    def _early_closes(self) -> frozenset[date]:
        return frozenset(d.date() for d in self._cal.early_closes)

    def is_trading_day(self, day: date) -> bool:
        return bool(self._cal.is_session(day.isoformat()))

    def trading_day(self, day: date) -> TradingDay:
        cached = self._days.get(day)
        if cached is not None:
            return cached
        if not self.is_trading_day(day):
            raise ValueError(f"{day} 不是交易日")
        label = day.isoformat()
        early = day in self._early_closes
        open_ = to_ns(self._cal.session_open(label).to_pydatetime())
        close = to_ns(self._cal.session_close(label).to_pydatetime())
        pre_open = ny_to_ns(day, PRE_OPEN)
        sessions = []
        if self.overnight_from is not None and day >= self.overnight_from:
            prev = day - timedelta(days=1)  # 周一的夜盘从周日 21:00 开始
            sessions.append(Session("overnight", ny_to_ns(prev, OVERNIGHT_OPEN), pre_open))
        sessions += [
            Session("pre", pre_open, open_),
            Session("regular", open_, close),
            Session("post", close, ny_to_ns(day, POST_CLOSE_EARLY if early else POST_CLOSE)),
        ]
        td = TradingDay(day=day, sessions=tuple(sessions), early_close=early)
        self._days[day] = td
        return td

    def trading_days(self, start: date, end: date) -> list[TradingDay]:
        """交易日期在 [start, end] 内的全部交易日。"""
        if end < start:
            return []
        labels = self._cal.sessions_in_range(start.isoformat(), end.isoformat())
        return [self.trading_day(d.date()) for d in labels]

    def days_overlapping(self, start: int, end: int) -> list[TradingDay]:
        """时间范围 [start, end)（UTC 纳秒）涉及的交易日。"""
        if end <= start:
            return []
        # 夜盘让交易日最早从前一天 21:00 开始，所以多看一天
        first = ny_date(start)
        last = ny_date(end - 1) + timedelta(days=1)
        return [d for d in self.trading_days(first, last) if d.start < end and d.end > start]

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

    def locate(self, ts: int) -> tuple[TradingDay, Session] | None:
        """ts 所在的交易日和时段；不在任何时段内（例如 20:00–21:00、周末、节假日）返回 None。"""
        for d in self.days_overlapping(ts, ts + 1):
            s = d.session_at(ts)
            if s is not None:
                return d, s
        return None

    def trading_date_of(self, ts: int) -> date | None:
        loc = self.locate(ts)
        return loc[0].day if loc else None

    def session_of(self, ts: int) -> SessionName | None:
        """ts 所在的时段名；不在任何时段内返回 None。"""
        loc = self.locate(ts)
        return loc[1].name if loc else None
