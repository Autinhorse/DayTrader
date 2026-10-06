"""数据层：存储、校验、catalog、下载（用假客户端，不联网）、历史查询、回放、拆股复权。"""

from __future__ import annotations

import threading
from datetime import date, time

import polars as pl
import pytest

from trader.core.clock import SimClock
from trader.core.scheduler import EventScheduler
from trader.core.timeutil import NS_PER_HOUR, NS_PER_SEC, ny_to_ns
from trader.core.trading_calendar import TradingCalendar
from trader.data.catalog import Catalog
from trader.data.corporate_actions import ACTION_SCHEMA, replace_symbol_actions
from trader.data.download import complete_days, download_symbol
from trader.data.history import HistoryService
from trader.data.massive import Cancelled, MassiveError
from trader.data.replay import HistoricalBarFeed
from trader.data.store import BAR_SCHEMA, BarStore, fingerprint
from trader.data.validate import validate_day

CAL = TradingCalendar()
DAY = date(2026, 1, 5)  # 周一，普通交易日
TD = CAL.trading_day(DAY)


def bars_at(times: list[time], day: date = DAY, price: float = 100.0) -> pl.DataFrame:
    ts = [ny_to_ns(day, t) for t in times]
    n = len(ts)
    return pl.DataFrame(
        {
            "ts_start": ts,
            "open": [price] * n,
            "high": [price + 1] * n,
            "low": [price - 1] * n,
            "close": [price] * n,
            "volume": [100.0] * n,
            "vwap": [price] * n,
            "trades": [3] * n,
        },
        schema=BAR_SCHEMA,
    )


def full_rth(day: date = DAY, price: float = 100.0) -> pl.DataFrame:
    """常规时段每 30 秒一根，外加一根盘前一根盘后。"""
    times = [time(4, 0)]
    for m in range(9 * 60 + 30, 16 * 60):
        times += [time(m // 60, m % 60, 0), time(m // 60, m % 60, 30)]
    times.append(time(19, 59, 59))
    return bars_at(times, day, price)


# ---------- 校验 ----------


def test_validate_clean_day():
    check = validate_day(full_rth(), TD)
    assert check.ok and check.warnings == {} and check.gaps == []


def test_validate_errors():
    df = bars_at([time(10, 0), time(10, 0), time(9, 59)])
    check = validate_day(df, TD)
    assert check.errors == {"duplicate_ts": 1, "unsorted": 1}
    bad = bars_at([time(10, 0)]).with_columns(pl.lit(0.0).alias("low"))
    assert validate_day(bad, TD).errors == {"nonpositive_price": 1}


def test_validate_warnings_and_gaps():
    df = full_rth().filter(
        ~pl.col("ts_start").is_between(ny_to_ns(DAY, time(11, 0)), ny_to_ns(DAY, time(11, 5)))
    )
    df = df.with_columns(
        pl.when(pl.col("ts_start") == ny_to_ns(DAY, time(12, 0)))
        .then(200.0)
        .otherwise(pl.col("vwap"))
        .alias("vwap")
    )
    check = validate_day(df, TD)
    assert check.ok
    assert check.warnings["vwap_outside_range"] == 1
    assert check.gaps == [(ny_to_ns(DAY, time(10, 59, 31)), ny_to_ns(DAY, time(11, 5, 30)))]


def test_validate_gap_before_first_bar():
    df = bars_at([time(9, 45), time(15, 59, 59)])
    gaps = validate_day(df, TD).gaps
    assert gaps[0] == (TD.open, ny_to_ns(DAY, time(9, 45)))


# ---------- 存储与 catalog ----------


def test_store_round_trip_and_fingerprint(tmp_path):
    store = BarStore(tmp_path)
    df = full_rth()
    store.write_day("SPY", DAY, df)
    back = store.read_day("SPY", DAY)
    assert back.equals(df)
    assert fingerprint(back) == fingerprint(df)
    assert fingerprint(df.with_columns(pl.col("close") + 0.01)) != fingerprint(df)
    assert store.symbols() == ["SPY"]
    assert not list(store.root.rglob("*.tmp"))


# ---------- 下载（假客户端） ----------


class FakeClient:
    def __init__(self, data: dict[tuple[str, date], pl.DataFrame], fail: set[date] | None = None):
        self.data = data
        self.fail = fail or set()
        self.calls: list[tuple[str, date]] = []
        self.stop_event = threading.Event()

    def bars_1s(self, symbol: str, day: date) -> pl.DataFrame:
        if self.stop_event.is_set():
            raise Cancelled()
        self.calls.append((symbol, day))
        if day in self.fail:
            raise MassiveError("HTTP 500")
        return self.data.get((symbol, day), pl.DataFrame(schema=BAR_SCHEMA))


def env(tmp_path, now: int):
    clock = SimClock(EventScheduler(), start=now)
    return BarStore(tmp_path), Catalog(tmp_path / "catalog.sqlite"), clock


def test_complete_days_excludes_unfinished_today():
    days = [date(2026, 1, 5), date(2026, 1, 6)]
    before = ny_to_ns(date(2026, 1, 6), time(15, 0))
    after = ny_to_ns(date(2026, 1, 6), time(20, 31))
    assert complete_days(CAL, days[0], days[1], before) == [days[0]]
    assert complete_days(CAL, days[0], days[1], after) == days


def test_download_incremental(tmp_path):
    d1, d2, d3 = date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)
    data = {("SPY", d1): full_rth(d1), ("SPY", d3): full_rth(d3)}  # d2 没有数据
    client = FakeClient(data, fail={d3})
    store, catalog, clock = env(tmp_path, ny_to_ns(date(2026, 1, 9), time(12)))
    log: list[str] = []

    def run(start: date):
        return download_symbol(
            client, store, catalog, CAL, clock, "SPY", start, d3, workers=2, log=log.append
        )

    res = run(d1)
    assert (res.stored, res.empty, len(res.failed)) == (1, 1, 1)
    assert store.has_day("SPY", d1) and not store.has_day("SPY", d2)
    assert catalog.known_days("SPY") == {d1, d2}

    # 再跑一次：只重试失败的那天
    client.fail = set()
    client.calls.clear()
    res = run(d1)
    assert client.calls == [("SPY", d3)] and res.stored == 1

    # 往前补一段：已有日期不重复下载
    client.calls.clear()
    run(date(2026, 1, 2))
    assert client.calls == [("SPY", date(2026, 1, 2))]


def test_download_rejects_invalid_day(tmp_path):
    bad = bars_at([time(10, 0), time(10, 0)])
    client = FakeClient({("SPY", DAY): bad})
    store, catalog, clock = env(tmp_path, ny_to_ns(date(2026, 1, 9), time(12)))
    res = download_symbol(client, store, catalog, CAL, clock, "SPY", DAY, DAY, log=lambda _m: None)
    assert len(res.rejected) == 1
    assert not store.has_day("SPY", DAY) and catalog.known_days("SPY") == set()


def test_download_stop(tmp_path):
    client = FakeClient({})
    client.stop_event.set()
    store, catalog, clock = env(tmp_path, ny_to_ns(date(2026, 1, 9), time(12)))
    res = download_symbol(
        client, store, catalog, CAL, clock, "SPY", DAY, date(2026, 1, 8), log=lambda _m: None
    )
    assert res.cancelled and catalog.known_days("SPY") == set()


# ---------- 历史查询与复权 ----------


@pytest.fixture
def history(tmp_path):
    store = BarStore(tmp_path)
    catalog = Catalog(tmp_path / "catalog.sqlite")
    for sym, price in (("AAA", 100.0), ("BBB", 50.0)):
        for d in (date(2026, 1, 5), date(2026, 1, 6)):
            df = full_rth(d, price)
            store.write_day(sym, d, df)
            catalog.record(sym, d, validate_day(df, CAL.trading_day(d)), fingerprint(df), 0)
    return HistoryService(tmp_path, CAL, catalog)


def test_history_session_filter_and_range(history):
    start = ny_to_ns(date(2026, 1, 5), time(0))
    end = ny_to_ns(date(2026, 1, 7), time(0))
    rth = history.bars("AAA", "1s", start, end)
    ext = history.bars("AAA", "1s", start, end, session="extended")
    assert rth.height == 2 * 780 and ext.height == 2 * 782
    part = history.bars("AAA", "1s", TD.open, TD.open + NS_PER_HOUR)
    assert part.height == 120 and part["ts_start"].max() < TD.open + NS_PER_HOUR
    assert history.coverage("AAA") == [date(2026, 1, 5), date(2026, 1, 6)]
    with pytest.raises(NotImplementedError):
        history.bars("AAA", "1m", start, end)


def test_split_adjustment(history):
    actions = pl.DataFrame(
        [
            {
                "symbol": "AAA",
                "kind": "split",
                "ex_date": date(2026, 1, 6),
                "ratio": 4.0,
                "cash_amount": None,
            }
        ],
        schema=ACTION_SCHEMA,
    )
    replace_symbol_actions(history.data_dir, "AAA", actions)
    start = ny_to_ns(date(2026, 1, 5), time(0))
    end = ny_to_ns(date(2026, 1, 7), time(0))
    adj = history.bars("AAA", "1s", start, end, adjusted=True)
    first_day = adj.filter(pl.col("ts_start") < ny_to_ns(date(2026, 1, 6), time(0)))
    second_day = adj.filter(pl.col("ts_start") >= ny_to_ns(date(2026, 1, 6), time(0)))
    assert first_day["close"].unique().to_list() == [25.0]
    assert first_day["volume"].unique().to_list() == [400.0]
    assert second_day["close"].unique().to_list() == [100.0]
    raw = history.bars("AAA", "1s", start, end)
    assert raw["close"].unique().to_list() == [100.0]  # 默认返回原始价格


# ---------- 回放 ----------


def test_replay_merges_symbols_in_time_order(history):
    feed = HistoricalBarFeed(history)
    feed.subscribe(["BBB", "AAA"], {"bar_1s"})
    events = list(feed.events(TD.open, TD.open + 60 * NS_PER_SEC))
    assert [(e.bar.symbol, e.bar.ts_start - TD.open) for e in events] == [
        ("BBB", 0),
        ("AAA", 0),
        ("BBB", 30 * NS_PER_SEC),
        ("AAA", 30 * NS_PER_SEC),
    ]
    assert all(e.meta.available_time == e.bar.ts_end for e in events)
    assert [e.meta.sequence for e in events] == [0, 1, 2, 3]


def test_replay_across_days_and_delay(history):
    feed = HistoricalBarFeed(history, decision_delay_ns=5)
    feed.subscribe(["AAA"], {"bar_1s"})
    start = ny_to_ns(date(2026, 1, 5), time(0))
    end = ny_to_ns(date(2026, 1, 7), time(0))
    events = list(feed.events(start, end))
    assert len(events) == 2 * 780
    times = [e.meta.available_time for e in events]
    assert times == sorted(times)
    assert events[0].meta.available_time == events[0].bar.ts_end + 5
    with pytest.raises(ValueError):
        feed.subscribe(["AAA"], {"tick"})
