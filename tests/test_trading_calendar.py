from datetime import UTC, date, datetime, time

import pytest

from trader.core.events import SessionKind
from trader.core.timeutil import from_ns, ny_to_ns
from trader.core.trading_calendar import TradingCalendar


@pytest.fixture(scope="module")
def cal() -> TradingCalendar:
    return TradingCalendar()


def ny_hm(ns: int) -> tuple[int, int]:
    dt = from_ns(ns)
    return dt.hour, dt.minute


def test_holidays_and_weekends(cal):
    assert cal.is_trading_day(date(2025, 12, 24))
    assert not cal.is_trading_day(date(2025, 12, 25))  # 圣诞
    assert not cal.is_trading_day(date(2025, 7, 4))  # 独立日
    assert not cal.is_trading_day(date(2026, 10, 3))  # 周六
    with pytest.raises(ValueError):
        cal.trading_day(date(2025, 12, 25))


def test_regular_day(cal):
    d = cal.trading_day(date(2026, 1, 5))
    assert not d.early_close
    assert [ny_hm(t) for t in (d.pre_open, d.open, d.close, d.post_close)] == [
        (4, 0),
        (9, 30),
        (16, 0),
        (20, 0),
    ]


def test_early_close(cal):
    d = cal.trading_day(date(2025, 11, 28))  # 感恩节次日
    assert d.early_close
    assert ny_hm(d.close) == (13, 0)
    assert ny_hm(d.post_close) == (17, 0)


def test_open_in_utc_follows_dst(cal):
    winter = cal.trading_day(date(2026, 1, 5))
    summer = cal.trading_day(date(2026, 7, 6))
    assert from_ns(winter.open, UTC).hour == 14
    assert from_ns(summer.open, UTC).hour == 13


def test_session_of(cal):
    day = date(2025, 11, 28)
    assert cal.session_of(ny_to_ns(day, time(3, 59))) is None
    assert cal.session_of(ny_to_ns(day, time(4, 0))) == "pre"
    assert cal.session_of(ny_to_ns(day, time(9, 30))) == "regular"
    assert cal.session_of(ny_to_ns(day, time(12, 59))) == "regular"
    assert cal.session_of(ny_to_ns(day, time(13, 0))) == "post"  # 半日市 13:00 后是盘后
    assert cal.session_of(ny_to_ns(day, time(17, 0))) is None
    assert cal.session_of(ny_to_ns(date(2025, 12, 25), time(10, 0))) is None


def test_session_events_in_order(cal):
    events = cal.trading_day(date(2026, 1, 5)).session_events()
    assert [e.kind for e in events] == [k for k in SessionKind if k != SessionKind.OVERNIGHT_OPEN]
    assert [e.ts for e in events] == sorted(e.ts for e in events)


def test_range_and_neighbours(cal):
    days = cal.trading_days(date(2025, 12, 22), date(2025, 12, 28))
    assert [d.day.day for d in days] == [22, 23, 24, 26]
    assert cal.next_trading_day(date(2025, 12, 24)) == date(2025, 12, 26)
    assert cal.previous_trading_day(date(2026, 1, 5)) == date(2026, 1, 2)


def test_session_of_uses_ny_date(cal):
    # 2026-01-06 00:30 UTC 是纽约 1 月 5 日 19:30，属于 1 月 5 日盘后
    ts = int(datetime(2026, 1, 6, 0, 30, tzinfo=UTC).timestamp()) * 1_000_000_000
    assert cal.session_of(ts) == "post"


# ---------- 夜盘（23/5） ----------


def test_overnight_sessions_and_trading_date():
    cal = TradingCalendar(overnight_from=date(2026, 12, 7))
    mon = cal.trading_day(date(2026, 12, 7))
    assert mon.has_overnight and [s.name for s in mon.sessions] == [
        "overnight",
        "pre",
        "regular",
        "post",
    ]
    # 周一的夜盘从周日 21:00 开始
    sun_2130 = ny_to_ns(date(2026, 12, 6), time(21, 30))
    assert mon.start == ny_to_ns(date(2026, 12, 6), time(21, 0))
    assert cal.trading_date_of(sun_2130) == date(2026, 12, 7)
    assert cal.session_of(sun_2130) == "overnight"
    # 周一 20:30 是暂停时间；21:00 起属于周二
    assert cal.session_of(ny_to_ns(date(2026, 12, 7), time(20, 30))) is None
    assert cal.trading_date_of(ny_to_ns(date(2026, 12, 7), time(21, 0))) == date(2026, 12, 8)
    # 夜盘开始之前的日子没有夜盘
    assert not cal.trading_day(date(2026, 12, 4)).has_overnight
    assert cal.trading_day(date(2026, 12, 7)).session_events()[0].kind == SessionKind.OVERNIGHT_OPEN


def test_days_overlapping():
    cal = TradingCalendar()
    start = ny_to_ns(date(2025, 12, 24), time(12))
    end = ny_to_ns(date(2025, 12, 26), time(5))
    assert [d.day for d in cal.days_overlapping(start, end)] == [
        date(2025, 12, 24),
        date(2025, 12, 26),
    ]
