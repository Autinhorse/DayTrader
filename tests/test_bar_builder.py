"""实时 1 秒 bar 生成：封口、迟到修订、unreported 规则、快照降级，
以及与离线聚合的一致性（录制重放）。
"""

from __future__ import annotations

from datetime import date, time
from pathlib import Path

import polars as pl
import pytest

from trader.core.timeutil import NS_PER_SEC, ny_to_ns
from trader.core.trading_calendar import TradingCalendar
from trader.data.bar_builder import SnapshotBarBuilder, TickBarBuilder, TradeTick
from trader.data.compare import bars_from_trades

CAL = TradingCalendar()
T0 = ny_to_ns(date(2026, 10, 6), time(10, 0))  # 常规时段内
MS = 10**6


def tick(sec: int, price: float, size: float = 100, recv_ms: int = 200, **kw) -> TradeTick:
    ts = T0 + sec * NS_PER_SEC
    return TradeTick("SPY", ts, price, size, ts + NS_PER_SEC + recv_ms * MS, **kw)


def test_seal_by_clock_with_grace():
    b = TickBarBuilder("SPY", CAL, grace_ns=1000 * MS)
    b.add(tick(0, 10.0))
    b.add(tick(0, 10.5))
    b.add(tick(0, 9.9, unreported=True, size=3))
    # 秒 0 结束于 T0+1s，宽限 1 秒：T0+2s 之前不封口
    assert b.seal(T0 + 2 * NS_PER_SEC - 1) == []
    out = b.seal(T0 + 2 * NS_PER_SEC)
    assert len(out) == 1
    bar = out[0].bar
    assert (bar.open, bar.high, bar.low, bar.close) == (
        10.0,
        10.5,
        10.0,
        10.5,
    )  # unreported 不更新价格
    assert bar.volume == 203 and bar.trades == 3 and bar.session == "regular"
    assert bar.ts_start == T0 and bar.ts_end == T0 + NS_PER_SEC


def test_sparse_seconds_and_only_unreported_second():
    b = TickBarBuilder("SPY", CAL)
    b.add(tick(0, 10.0))
    b.add(tick(1, 10.1, unreported=True))  # 只有 unreported：没有 bar
    b.add(tick(3, 10.2))
    out = b.seal(T0 + 10 * NS_PER_SEC)
    assert [x.bar.ts_start - T0 for x in out] == [0, 3 * NS_PER_SEC]


def test_late_tick_becomes_revision_not_change():
    b = TickBarBuilder("SPY", CAL, grace_ns=300 * MS)
    b.add(tick(0, 10.0))
    sent = b.seal(T0 + NS_PER_SEC + 300 * MS)
    assert len(sent) == 1
    b.add(tick(0, 11.0, recv_ms=800))  # 封口后才到
    assert sent[0].bar.high == 10.0  # 已发出的 bar 不变
    assert b.late_ticks == 1
    rev = b.revisions[-1]
    assert "late_revision" in rev.flags and rev.bar.high == 11.0 and rev.bar.close == 11.0


def test_handoff_boundary_drops_earlier_seconds():
    b = TickBarBuilder("SPY", CAL, start_ns=T0 + 5 * NS_PER_SEC)
    b.add(tick(4, 10.0))
    b.add(tick(5, 10.1))
    out = b.seal(T0 + 20 * NS_PER_SEC)
    assert [x.bar.ts_start for x in out] == [T0 + 5 * NS_PER_SEC]


def test_outside_sessions_dropped():
    night = ny_to_ns(date(2026, 10, 6), time(20, 30))
    b = TickBarBuilder("SPY", CAL)
    b.add(TradeTick("SPY", night, 10.0, 1, night))
    assert b.seal(night + 10 * NS_PER_SEC) == []


def test_snapshot_builder_volume_diff_and_degraded():
    b = SnapshotBarBuilder("NVDA", CAL)
    r = T0 + 100 * MS
    b.add(r, 100.0, 1_000)  # 第一次：只建立基准，价格算一次观测
    b.add(r + 100 * MS, 100.0, 1_000)  # 无变化（例如只是买卖价变了）
    b.add(r + 200 * MS, 100.2, 1_300)
    b.add(r + 300 * MS, 99.9, 1_350)
    b.add(r + NS_PER_SEC, 99.9, 1_350)  # 下一秒无变化：没有 bar
    out = b.seal(T0 + 5 * NS_PER_SEC)
    assert len(out) == 1
    bar = out[0].bar
    assert (bar.open, bar.high, bar.low, bar.close) == (100.0, 100.2, 99.9, 99.9)
    assert bar.volume == 350 and bar.vwap is None and bar.trades is None
    assert out[0].flags == frozenset({"degraded"})


RECORDING = Path(__file__).resolve().parents[1] / "data" / "live" / "2026-10-06" / "tbt.jsonl"


@pytest.mark.skipif(not RECORDING.exists(), reason="没有 2026-10-06 的 IBKR 录制")
def test_replay_recording_matches_offline_aggregation():
    """录制重放一致性：把录制的逐笔按接收顺序喂给实时生成器（足够大的宽限，没有迟到），
    结果必须与离线聚合（价格只用非 unreported 成交，成交量用全部成交）完全相同。"""
    lo = ny_to_ns(date(2026, 10, 6), time(14, 0))
    hi = lo + 10 * 60 * NS_PER_SEC
    t = (
        pl.scan_ndjson(RECORDING)
        .filter((pl.col("sym") == "NVDA") & (pl.col("ts") >= lo) & (pl.col("ts") < hi))
        .collect()
        .sort("recv", maintain_order=True)
    )
    assert t.height > 1000
    b = TickBarBuilder("NVDA", CAL, grace_ns=60 * NS_PER_SEC)
    live = []
    for r in t.iter_rows(named=True):
        b.add(TradeTick("NVDA", r["ts"], r["price"], r["size"], r["recv"], r["unreported"]))
        live += b.seal(r["recv"])
    live += b.seal(hi + 120 * NS_PER_SEC)
    assert b.late_ticks == 0
    got = pl.DataFrame(
        [
            (x.bar.ts_start, x.bar.open, x.bar.high, x.bar.low, x.bar.close, x.bar.volume)
            for x in live
        ],
        schema=["ts_start", "open", "high", "low", "close", "volume"],
        orient="row",
    )
    priced = bars_from_trades(t.filter(~pl.col("unreported")).select("ts", "price", "size"))
    vol = bars_from_trades(t.select("ts", "price", "size")).select("ts_start", "volume")
    want = priced.drop("volume").join(vol, on="ts_start", how="left")
    assert got.height == want.height
    assert got.sort("ts_start").equals(want.select(got.columns).sort("ts_start"))
