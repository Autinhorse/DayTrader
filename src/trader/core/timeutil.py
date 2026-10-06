"""时间约定（DESIGN.md 4.1）：内部统一用 UTC 纳秒整数，显示和交易时段判断用 America/New_York。

本模块只做换算，不读取系统时间；取当前时间只能通过 Clock。
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

NS_PER_US = 1_000
NS_PER_MS = 1_000_000
NS_PER_SEC = 1_000_000_000
NS_PER_MIN = 60 * NS_PER_SEC
NS_PER_HOUR = 60 * NS_PER_MIN
NS_PER_DAY = 24 * NS_PER_HOUR

_TIMEFRAME_UNITS = {"s": NS_PER_SEC, "m": NS_PER_MIN, "h": NS_PER_HOUR, "d": NS_PER_DAY}
_TIMEFRAME_RE = re.compile(r"^([1-9][0-9]*)([smhd])$")


def timeframe_ns(timeframe: str) -> int:
    """把 "1s" "5m" "1h" "1d" 这样的周期换算成纳秒。

    "1d" 只表示一个交易日的 bar，它的长度按 24 小时算，只用于排序和 ts_end 的约定；
    日线的实际交易时段由交易日历决定。
    """
    m = _TIMEFRAME_RE.match(timeframe)
    if m is None:
        raise ValueError(f"无法识别的周期：{timeframe!r}")
    return int(m.group(1)) * _TIMEFRAME_UNITS[m.group(2)]


def to_ns(dt: datetime) -> int:
    """带时区的 datetime → UTC 纳秒。拒绝不带时区的时间，避免按本机时区误解。"""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("datetime 必须带时区")
    delta = dt.astimezone(UTC) - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * NS_PER_SEC + delta.microseconds * NS_PER_US


def from_ns(ns: int, tz: tzinfo = NY) -> datetime:
    """UTC 纳秒 → 指定时区的 datetime（默认纽约时间）。微秒以下的部分被截断，只用于显示。"""
    return (_EPOCH + timedelta(microseconds=ns // NS_PER_US)).astimezone(tz)


def ny_to_ns(day: date, t: time) -> int:
    """纽约当地日期加时刻 → UTC 纳秒，自动处理夏令时。"""
    return to_ns(datetime.combine(day, t, tzinfo=NY))


def ny_date(ns: int) -> date:
    """UTC 纳秒所在的纽约日期。"""
    return from_ns(ns).date()
