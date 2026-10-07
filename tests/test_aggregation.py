"""聚合一致性（DESIGN.md 13）：增量聚合器与向量化实现结果相同；边界：跨时段、半日市、稀疏成交。"""

from __future__ import annotations

import random
from datetime import date, time
from pathlib import Path

import polars as pl
import pytest

from trader.core.aggregation import SUPPORTED_TIMEFRAMES, BarAggregator
from trader.core.models import Bar
from trader.core.timeutil import NS_PER_MIN, NS_PER_SEC, ny_to_ns
from trader.core.trading_calendar import TradingCalendar
from trader.data.aggregate import aggregate_day
from trader.data.store import BAR_SCHEMA, BarStore

CAL = TradingCalendar()
DAY = date(2026, 1, 5)
HALF = date(2025, 11, 28)  # 半日市


def random_day(day: date, n: int, seed: int = 1) -> pl.DataFrame:
    """在 04:00–20:00 之间随机取 n 秒生成 1 秒 bar（稀疏）。"""
    td = CAL.trading_day(day)
    rnd = random.Random(seed)
    secs = sorted(rnd.sample(range((td.end - td.start) // NS_PER_SEC), n))
    rows = []
    price = 100.0
    for s in secs:
        o = price
        c = round(o + rnd.uniform(-0.2, 0.2), 2)
        h = max(o, c) + round(rnd.uniform(0, 0.1), 2)
        lo = min(o, c) - round(rnd.uniform(0, 0.1), 2)
        v = float(rnd.randint(1, 500))
        rows.append((td.start + s * NS_PER_SEC, o, h, lo, c, v, (o + c) / 2, rnd.randint(1, 9)))
        price = c
    return pl.DataFrame(rows, schema=BAR_SCHEMA, orient="row")


def incremental(df: pl.DataFrame, timeframe: str, sessions) -> list[Bar]:
    agg = BarAggregator("X", timeframe, CAL, sessions)
    out: list[Bar] = []
    for ts, o, h, lo, c, v, vw, n in df.iter_rows():
        # 先按时钟收盘（模拟引擎在每个事件前推进时钟）
        closed = agg.on_time(ts)
        if closed:
            out.append(closed)
        out += agg.update(Bar("X", "1s", ts, o, h, lo, c, v, vw, n))
    last = agg.flush()
    if last:
        out.append(last)
    return out


def as_rows(bars: list[Bar]) -> list[tuple]:
    return [
        (b.ts_start, b.ts_end, b.open, b.high, b.low, b.close, b.volume, b.trades, b.session)
        for b in bars
    ]


def frame_rows(df: pl.DataFrame) -> list[tuple]:
    cols = ["ts_start", "ts_end", "open", "high", "low", "close", "volume", "trades", "session"]
    return [tuple(r) for r in df.select(cols).iter_rows()]


@pytest.mark.parametrize("timeframe", [tf for tf in SUPPORTED_TIMEFRAMES if tf != "1s"])
@pytest.mark.parametrize("sessions", ["extended", "rth"])
@pytest.mark.parametrize("day", [DAY, HALF])
def test_incremental_equals_vectorized(timeframe, sessions, day):
    df = random_day(day, 3000)
    inc = incremental(df, timeframe, sessions)
    vec = aggregate_day(df, CAL.trading_day(day), timeframe, sessions)
    assert as_rows(inc) == frame_rows(vec)
    # vwap 用浮点累加，顺序不同可能有极小差异
    for b, vw in zip(inc, vec["vwap"].to_list(), strict=True):
        assert b.vwap == pytest.approx(vw, rel=1e-12)


def test_hour_bars_split_at_session_boundaries():
    td = CAL.trading_day(DAY)
    df = random_day(DAY, 8000)
    out = aggregate_day(df, td, "1h", "extended")
    pre_last = out.filter(pl.col("session") == "pre").tail(1)
    assert pre_last["ts_start"].item() == ny_to_ns(DAY, time(9, 0))
    assert pre_last["ts_end"].item() == td.open  # 半根，09:30 收盘
    reg = out.filter(pl.col("session") == "regular")
    assert reg["ts_start"].head(1).item() == td.open  # 常规时段从 09:30 起算
    assert reg["ts_end"].tail(1).item() == td.close  # 15:30–16:00 半根


def test_half_day_bounds():
    td = CAL.trading_day(HALF)
    df = random_day(HALF, 3000)
    daily = aggregate_day(df, td, "1d", "rth")
    assert daily["ts_start"].item() == td.open and daily["ts_end"].item() == td.close
    assert ny_to_ns(HALF, time(13)) == td.close
    m30 = aggregate_day(df, td, "30m", "rth")
    assert m30["ts_end"].max() == td.close


def test_sparse_no_empty_bars_and_clock_close():
    """稀疏成交：没有成交的分钟没有 bar；bar 由时钟在收盘时刻封口，不等下一笔。"""
    td = CAL.trading_day(DAY)
    t1 = td.open + 5 * NS_PER_SEC
    t2 = td.open + 7 * NS_PER_MIN  # 中间 6 分钟没有成交
    agg = BarAggregator("X", "1m", CAL, "rth")
    assert agg.update(Bar("X", "1s", t1, 1, 1, 1, 1, 10)) == []
    assert agg.next_close_time() == td.open + NS_PER_MIN
    assert agg.on_time(td.open + NS_PER_MIN - 1) is None
    first = agg.on_time(td.open + NS_PER_MIN)
    assert first is not None and first.ts_start == td.open and first.ts_end == td.open + NS_PER_MIN
    assert agg.next_close_time() is None
    agg.update(Bar("X", "1s", t2, 2, 2, 2, 2, 10))
    second = agg.flush()
    assert second is not None and second.ts_start == td.open + 7 * NS_PER_MIN


def test_rth_filter_ignores_extended_bars():
    td = CAL.trading_day(DAY)
    agg = BarAggregator("X", "5m", CAL, "rth")
    assert agg.update(Bar("X", "1s", td.pre_open, 1, 1, 1, 1, 1)) == []
    assert agg.partial() is None


def test_partial_bar():
    td = CAL.trading_day(DAY)
    agg = BarAggregator("X", "1m", CAL, "rth")
    agg.update(Bar("X", "1s", td.open, 10, 11, 9, 10.5, 100, 10.2, 3))
    agg.update(Bar("X", "1s", td.open + NS_PER_SEC, 10.5, 12, 10, 11, 300, 11.0, 2))
    p = agg.partial()
    assert p is not None
    assert (p.open, p.high, p.low, p.close, p.volume, p.trades) == (10, 12, 9, 11, 400, 5)
    assert p.vwap == pytest.approx((10.2 * 100 + 11.0 * 300) / 400)


REAL = Path(__file__).resolve().parents[1] / "data"


@pytest.mark.skipif(not (REAL / "bars" / "1s").exists(), reason="本机没有真实数据")
@pytest.mark.parametrize("symbol", ["SPY", "KORU"])  # 一个活跃、一个稀疏
def test_real_day_incremental_equals_vectorized(symbol):
    df = BarStore(REAL).read_day(symbol, DAY)
    if df.is_empty():
        pytest.skip("没有这一天的数据")
    for timeframe in ("1m", "5m", "1h", "1d"):
        inc = incremental(df, timeframe, "extended")
        vec = aggregate_day(df, CAL.trading_day(DAY), timeframe, "extended")
        assert as_rows(inc) == frame_rows(vec)
