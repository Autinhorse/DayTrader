from datetime import UTC, date, datetime, time

import pytest

from trader.core.timeutil import (
    NS_PER_HOUR,
    NS_PER_SEC,
    from_ns,
    ny_date,
    ny_to_ns,
    timeframe_ns,
    to_ns,
)


@pytest.mark.parametrize(
    ("tf", "ns"),
    [("1s", NS_PER_SEC), ("5m", 300 * NS_PER_SEC), ("1h", NS_PER_HOUR), ("15m", 900 * NS_PER_SEC)],
)
def test_timeframe_ns(tf, ns):
    assert timeframe_ns(tf) == ns


@pytest.mark.parametrize("tf", ["", "0s", "1x", "m5", "1.5m"])
def test_timeframe_invalid(tf):
    with pytest.raises(ValueError):
        timeframe_ns(tf)


def test_to_ns_rejects_naive():
    with pytest.raises(ValueError):
        to_ns(datetime(2026, 1, 2, 9, 30))  # noqa: DTZ001


def test_round_trip_keeps_microseconds():
    dt = datetime(2026, 3, 9, 14, 30, 0, 123456, tzinfo=UTC)
    assert from_ns(to_ns(dt), UTC) == dt


def test_ny_open_across_dst():
    # 冬令时 9:30 纽约 = 14:30 UTC；夏令时 = 13:30 UTC
    assert from_ns(ny_to_ns(date(2026, 1, 5), time(9, 30)), UTC).hour == 14
    assert from_ns(ny_to_ns(date(2026, 7, 6), time(9, 30)), UTC).hour == 13


def test_ny_date_near_midnight_utc():
    # 2026-07-07 01:00 UTC 仍是纽约的 7 月 6 日晚上
    assert ny_date(to_ns(datetime(2026, 7, 7, 1, 0, tzinfo=UTC))) == date(2026, 7, 6)
