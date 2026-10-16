"""日内波动报表（trader.analytics，规范第 9 节）：摆动算法、ADR、最后一段、异常日、扩展时段、
复权输出、阈值敏感性、端到端。"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date, time

import matplotlib
import numpy as np
import pandas as pd
import polars as pl
import pytest

from conftest import CAL, make_dataset, synth_day
from trader.analytics import ReportParams, build_report
from trader.analytics.cleaning import (
    BadWindow,
    clean_day,
    clean_fine,
    load_bad_windows,
    resample_close,
)
from trader.analytics.daily_metrics import Bars, DayInput, compute_day, efficiency, vwap_crosses
from trader.analytics.extended_hours import EthDay, eth_metrics
from trader.analytics.loader import adr_series
from trader.analytics.quality import flag_anomalies, outlier_bars
from trader.analytics.swings import find_swings
from trader.data.catalog import Catalog
from trader.data.corporate_actions import replace_symbol_actions
from trader.data.history import HistoryService
from trader.data.store import BarStore, fingerprint
from trader.data.validate import validate_day

matplotlib.use("Agg")

M = 60 * 10**9


def point_bars(prices: Sequence[float]) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """每分钟一个价格点（开高低相同）。"""
    p = np.asarray(prices, dtype=float)
    return np.arange(len(p), dtype=np.int64) * M, p, p, p


def bars(
    rows: Sequence[tuple[float, float, float, float]], start: int = 0, vol: float = 100.0
) -> Bars:
    """rows: (open, high, low, close)，每分钟一根。"""
    a = np.asarray(rows, dtype=float)
    n = len(a)
    return Bars(
        ts=start + np.arange(n, dtype=np.int64) * M,
        open=a[:, 0],
        high=a[:, 1],
        low=a[:, 2],
        close=a[:, 3],
        volume=np.full(n, vol),
        vwap=a[:, 3].copy(),
    )


def flat(prices: Sequence[float], start: int = 0) -> Bars:
    return bars([(p, p, p, p) for p in prices], start)


# ---------- 摆动算法 ----------


def test_straight_line_has_no_complete_swing():
    run = find_swings(*point_bars([100 + 0.1 * i for i in range(390)]), theta=1.0)
    assert run.swings == [] and run.ambiguous == 0
    assert run.pivot == pytest.approx(100)  # 整天一段：从开盘价起算，未确认


def test_sine_wave_swings():
    # 周期 40 分钟、振幅 2，4 个周期：极值在第 10、30、…、150 分钟；第 160 分钟回到 100
    prices = [100 + 2 * math.sin(2 * math.pi * t / 40) for t in range(161)]
    sw = find_swings(*point_bars(prices), theta=1.0).swings
    assert len(sw) == 8  # 开盘 → 第一个高点，之后 7 段完整摆动；最后一段未确认不计
    assert sw[0].start_price == pytest.approx(100) and sw[0].amplitude == pytest.approx(2)
    assert all(s.amplitude == pytest.approx(4) for s in sw[1:])
    assert [s.direction for s in sw[:3]] == [1, -1, 1]
    assert sw[1].start_ts == 10 * M and sw[1].end_ts == 30 * M
    assert sw[1].confirm_ts > sw[1].end_ts


def test_random_walk_swings_alternate_and_exceed_threshold():
    rnd = np.random.default_rng(7)
    prices = list(100 + np.cumsum(rnd.normal(0, 0.1, 390)))
    sw = find_swings(*point_bars(prices), theta=0.5).swings
    assert len(sw) > 3
    assert all(s.amplitude >= 0.5 - 1e-9 for s in sw)
    assert all(a.direction == -b.direction for a, b in zip(sw, sw[1:], strict=False))
    assert all(a.end_ts == b.start_ts for a, b in zip(sw, sw[1:], strict=False))  # 首尾相接


def test_smaller_threshold_never_finds_fewer_swings():
    rnd = np.random.default_rng(3)
    for _ in range(200):
        c = 100 + np.cumsum(rnd.normal(0, 0.05, 390))
        o = np.r_[100, c[:-1]]
        h = np.maximum(o, c) + rnd.exponential(0.02, 390)
        lo = np.minimum(o, c) - rnd.exponential(0.02, 390)
        ts = np.arange(390, dtype=np.int64) * M
        counts = [len(find_swings(ts, o, h, lo, th).swings) for th in (0.1, 0.15, 0.25, 0.5)]
        assert counts == sorted(counts, reverse=True)


def test_first_swing_starts_from_low_when_open_dips_first():
    # ADR 10，阈值 0.25 ADR = 2.5：开盘 100，先跌 0.2 ADR 到 98，再涨到 105，回落到 101 确认
    prices = [100, 99, 98, 100, 102, 105, 103, 101]
    sw = find_swings(*point_bars(prices), theta=2.5).swings
    assert len(sw) == 1
    assert sw[0].start_price == 98 and sw[0].end_price == 105 and sw[0].start_ts == 2 * M


def _conflict_minute():
    # 第 2 分钟同时满足向上确认方向和向下回撤：先到 103 还是先到 99 结果不同
    rows = [(100, 100.5, 99.5, 100), (100, 103, 99, 101), (101, 101, 100.5, 101)]
    a = np.asarray(rows, dtype=float)
    ts = np.arange(3, dtype=np.int64) * M
    return ts, a[:, 0], a[:, 1], a[:, 2]


def _fine(order: str):
    def fine(t0: int, t1: int):
        if t0 != M:
            return None
        # 两根 1 秒 bar：先到 103 再到 99，或者先到 99 再到 103
        if order == "high_first":
            o, h, lo = [100.0, 99.0], [103.0, 99.0], [102.0, 99.0]
        else:
            o, h, lo = [100.0, 103.0], [100.0, 103.0], [99.0, 103.0]
        return np.array([t0, t0 + 10**9]), np.array(o), np.array(h), np.array(lo)

    return fine


def test_same_minute_conflict_resolved_with_1s_bars():
    ts, o, h, lo = _conflict_minute()
    up_first = find_swings(ts, o, h, lo, 2.0, fine=_fine("high_first"))
    down_first = find_swings(ts, o, h, lo, 2.0, fine=_fine("low_first"))
    assert up_first.ambiguous == 0 and down_first.ambiguous == 0
    # 先涨：开盘区间低点 99.5 → 103 确认，再从 99 反弹 2 确认 103 → 99
    assert [(s.start_price, s.end_price) for s in up_first.swings] == [(99.5, 103), (103, 99)]
    # 先跌：方向未定时到 99，再涨到 103 确定向上；第 3 分钟回落 2.5 确认 99 → 103
    assert [(s.start_price, s.end_price) for s in down_first.swings] == [(99, 103)]


def test_same_minute_conflict_without_1s_bars_is_conservative():
    ts, o, h, lo = _conflict_minute()
    run = find_swings(ts, o, h, lo, 2.0, fine=None)
    assert run.ambiguous == 1
    assert [(s.start_price, s.end_price) for s in run.swings] == [(99, 103)]  # 取确认较少的顺序


# ---------- 最后一段 ----------


def test_last_leg_counts_the_closing_trend_but_not_as_a_swing():
    # 开盘后小幅震荡（振幅 1，阈值 0.5），之后单边下跌到收盘
    prices = [100, 101, 100, 101, 100, 101, *[100 - i for i in range(11)]]
    open_ = 14 * 3600 * 10**9
    m1 = flat(prices, open_)
    inp = DayInput(open_, open_ + len(prices) * M, m1, m1, adr=5.0, pc=100.0)
    res = compute_day(
        inp, ReportParams(swing_thresholds_adr=(0.1,), score_k=0.1, swing_skip_open_min=0)
    )
    met = res.metrics
    # 前 7 分钟 100、101 交替：5 次完整摆动；最后一个转折点是 101（第 5 分钟），之后一路跌到 90
    assert met["swing_n_k010"] == 5
    assert met["last_leg_pct_k010"] == pytest.approx((90 - 101) / 101 * 100)
    assert met["last_leg_adr_k010"] == pytest.approx((90 - 101) / 5)
    leg = res.last_legs[0.1]
    assert leg.start_price == 101 and leg.end_price == 90 and leg.start_ts == open_ + 5 * M


def test_swings_in_the_opening_minutes_are_reported_separately():
    # 摆动起点在第 0、1、2、3、4 分钟（开盘段）和之后 30 分钟后的一段来回
    prices = [100, 101, 100, 101, 100, 101, *[101] * 25, 100, 101, 100]
    open_ = 14 * 3600 * 10**9
    m1 = flat(prices, open_)
    inp = DayInput(open_, open_ + len(prices) * M, m1, m1, adr=5.0, pc=100.0)
    p = ReportParams(swing_thresholds_adr=(0.1,), score_k=0.1, open_skip_compare=(0, 5, 30))
    met = compute_day(inp, p).metrics
    # 全部完整摆动：开盘段 5 次（起点 0–4 分钟），之后 101→100、100→101 两次（起点 5、30 分钟）
    assert met["swing_n_k010_s00"] == 7
    assert met["open_swing_n"] == 5 and met["swing_n_k010"] == 2 == met["swing_n_k010_s05"]
    assert met["swing_n_k010_s30"] == 1
    assert met["swing_capturable_pct_k010_s00"] >= met["swing_capturable_pct_k010"]


# ---------- ADR ----------


def test_adr_median_and_no_lookahead():
    r = np.array([2, 4, 9, 8.0])
    adr = adr_series(r, 3)
    assert np.isnan(adr[:2]).all()
    # 中位数：一个大行情日（9）不会把 ADR 拉高
    assert adr[2] == pytest.approx(4) and adr[3] == pytest.approx(8)
    r2 = r.copy()
    r2[-1] = 100  # 改动最后一天不影响之前的值
    assert np.array_equal(adr_series(r2, 3)[:3], adr[:3], equal_nan=True)


def test_adr_skips_early_close_days():
    adr = adr_series(np.array([2, 4, np.nan, 7.0]), 3)  # 第 3 天提前收盘：不计入、不占名额
    assert np.isnan(adr[2]) and adr[3] == pytest.approx(4)


def _write_days(root, sym: str, days: list[date], starts: list[float]) -> HistoryService:
    store = BarStore(root)
    catalog = Catalog(root / "catalog.sqlite")
    for j, d in enumerate(days):
        td = CAL.trading_day(d)
        df = synth_day(td, 50 + j, None, starts[j])
        store.write_day(sym, d, df)
        catalog.record(sym, d, validate_day(df, td), fingerprint(df), 0)
    return HistoryService(root, CAL, catalog)


def test_adr_ignores_overnight_gap(tmp_path):
    days = [date(2026, 1, d) for d in (5, 6, 7, 8)]
    # 第 3 天开盘大幅跳空，但日内路径（同一随机种子）和振幅不变
    a = _write_days(tmp_path / "a", "AAA", days, [100, 100, 100, 100])
    b = _write_days(tmp_path / "b", "AAA", days, [100, 100, 130, 100])
    kw = {"benchmark_tickers": (), "adr_days": 2, "include_extended_hours": False}
    da = build_report(["AAA"], "2026-01-07", "2026-01-08", history=a, **kw).daily
    db = build_report(["AAA"], "2026-01-07", "2026-01-08", history=b, **kw).daily
    assert db["adr"].to_numpy() == pytest.approx(da["adr"].to_numpy(), rel=0.02)
    assert abs(db["gap_adr"].iloc[0]) > 5 * abs(da["gap_adr"].iloc[0])  # 跳空本身看得到


def test_split_output_prices_and_adr_are_continuous(tmp_path):
    days = [date(2026, 1, d) for d in (5, 6, 7, 8, 9)]
    # 1 月 8 日 2 比 1 拆股：原始价格从约 100 变成约 50
    h = _write_days(tmp_path, "AAA", days, [100, 100, 100, 50, 50])
    actions = pl.DataFrame(
        {"symbol": ["AAA"], "kind": ["split"], "ex_date": [date(2026, 1, 8)], "ratio": [2.0],
         "cash_amount": [None]},
        schema_overrides={"cash_amount": pl.Float64},
    )  # fmt: skip
    replace_symbol_actions(tmp_path, "AAA", actions)
    rep = build_report(
        ["AAA"], "2026-01-07", "2026-01-09", history=h, benchmark_tickers=(), adr_days=2,
        include_extended_hours=False,
    )  # fmt: skip
    d = rep.daily.set_index("date")
    # 复权到 1 月 9 日：拆股前的价格除以 2，前后连续
    assert d.at[date(2026, 1, 7), "close"] < 60
    assert abs(d.at[date(2026, 1, 8), "open"] / d.at[date(2026, 1, 7), "close"] - 1) < 0.05
    adr = d["adr"].to_numpy()
    assert 0.5 < adr[1] / adr[0] < 2 and 0.5 < adr[2] / adr[1] < 2
    assert d.at[date(2026, 1, 7), "raw_open"] > 90  # 成本按当天实际价格分档
    assert list(rep.splits["symbol"]) == ["AAA"]


# ---------- 其他指标 ----------


def test_efficiency_line_and_round_trip():
    assert efficiency(100, np.array([101, 102, 103, 104.0])) == pytest.approx(1)
    back = np.array([101, 102, 101, 100, 101, 102, 101, 100.0])
    assert efficiency(100, back) == pytest.approx(0)


def test_missing_minutes_not_filled():
    open_ = 14 * 3600 * 10**9
    rows = [(100, 100, 100, 100), (100, 101, 100, 101), (101, 102, 101, 102)]
    m1 = bars(rows, open_)
    m1 = Bars(
        ts=np.array([0, 1, 9]) * M + open_,  # 第 2–8 分钟没有成交
        open=m1.open, high=m1.high, low=m1.low, close=m1.close, volume=m1.volume, vwap=m1.vwap,
    )  # fmt: skip
    inp = DayInput(open_, open_ + 10 * M, m1, m1, adr=4.0, pc=99.0)
    met = compute_day(inp, ReportParams(swing_thresholds_adr=(0.25,), score_k=0.25)).metrics
    r = np.diff(np.log([100, 101, 102]))
    assert met["rv_5min_pct"] == pytest.approx(math.sqrt((r**2).sum()) * 100)  # 没有填充出来的 0
    assert met["missing_ratio"] == pytest.approx(0.7)
    assert met["max_gap_min"] == 7
    assert met["gap_adr"] == pytest.approx(0.25)


def test_vwap_crosses_need_hold():
    # VWAP 固定 100（vwap 列是 100、成交量相同）：收盘 101,99,99,99,101,99,99,99
    closes = [101, 99, 99, 99, 101, 99, 99, 99, 99]
    m1 = flat(closes)
    vwap = np.full(len(closes), 100.0)
    assert vwap_crosses(m1, vwap, 3, len(closes) * M) == 1  # 第 5 分钟回到上方只停留 1 分钟，不算
    assert vwap_crosses(m1, vwap, 1, len(closes) * M) == 3


# ---------- 异常日 ----------


def test_outlier_bar_is_detected():
    prices = [100 + 0.01 * (i % 3) for i in range(30)]
    m1 = flat(prices)
    assert outlier_bars(m1, 0.5) == 0
    rows = [(p, p, p, p) for p in prices]
    rows[20] = (100, 100.02, 98.0, 100.01)  # 一笔离群的低价
    assert outlier_bars(bars(rows), 0.5) == 1
    assert outlier_bars(bars(rows), 0.5, skip_before=25 * M) == 0  # 开盘头几分钟不判断


def test_flag_two_kinds_of_days():
    n = [10.0] * 9 + [50.0]
    daily = pd.DataFrame(
        {
            "symbol": ["A"] * 10,
            "included": [True] * 10,
            "swing_n_k025": n,
            "swing_density": [3.0] * 8 + [8.0, 3.0],
            "outlier_bars": [0.0] * 6 + [1.0, 0.0, 0.0, 0.0],
            "window_minutes": [3.0] + [0.0] * 9,
            "changed_minutes": [0.0, 2.0] + [0.0] * 8,
            "fixed_prints": [0.0, 5.0] + [0.0] * 8,
        }
    )
    out = flag_anomalies(daily, ReportParams())
    assert out["data_issue"].tolist() == [True, True] + [False] * 8
    assert "坏数据时段" in out.at[0, "data_reason"]
    assert "修正离群成交 5 笔" in out.at[1, "data_reason"]
    assert out["special_day"].tolist() == [False] * 6 + [True, False, True, True]
    assert "价格尖刺" in out.at[6, "special_reason"]
    assert "摆动密度" in out.at[8, "special_reason"]
    assert "摆动次数 50" in out.at[9, "special_reason"]


# ---------- 数据清洗 ----------


def _secs(prices: Sequence[float], start: int = 0) -> Bars:
    p = np.asarray(prices, dtype=float)
    n = len(p)
    return Bars(
        ts=start + np.arange(n, dtype=np.int64) * 10**9, open=p.copy(), high=p + 0.01,
        low=p - 0.01, close=p.copy(), volume=np.full(n, 100.0), vwap=p.copy(),
    )  # fmt: skip


def test_clean_fine_fixes_isolated_bad_print_but_keeps_trend():
    b = _secs([100.0] * 60)
    lo = b.low.copy()
    lo[30] = 95.0  # 一笔离群成交：最低价 95，开收盘正常
    b = Bars(b.ts, b.open, b.high, lo, b.close, b.volume, b.vwap)
    out, fixed = clean_fine(b, 1.0)
    assert fixed == 1 and out.low.min() == pytest.approx(99.99) and len(out) == 60
    bad = _secs([100.0] * 30 + [95.0] + [100.0] * 29)  # 开收盘都离群：整根删掉
    out2, fixed2 = clean_fine(bad, 1.0)
    assert fixed2 == 1 and len(out2) == 59
    trend = _secs(list(np.linspace(100, 110, 120)))  # 两分钟涨 10，远超阈值，但不是离群
    out3, fixed3 = clean_fine(trend, 1.0)
    assert fixed3 == 0 and len(out3) == 120


def test_rebuild_minutes_and_bad_windows(tmp_path):
    fine = _secs([100.0] * 180)  # 3 分钟
    lo = fine.low.copy()
    lo[70] = 90.0
    fine = Bars(fine.ts, fine.open, fine.high, lo, fine.close, fine.volume, fine.vwap)
    m1 = Bars(
        ts=np.arange(3, dtype=np.int64) * M, open=np.full(3, 100.0), high=np.full(3, 100.01),
        low=np.array([99.99, 90.0, 99.99]), close=np.full(3, 100.0), volume=np.full(3, 1e4),
        vwap=np.full(3, 100.0),
    )  # fmt: skip
    res = clean_day(m1, fine, 1.0, [])
    assert res.m1.low.min() == pytest.approx(99.99) and res.changed_minutes == 1
    assert res.m1.volume.tolist() == [1e4] * 3  # 成交量沿用原来的 1 分钟 bar
    res2 = clean_day(m1, fine, 1.0, [(M, 2 * M)])  # 第 2 分钟在坏数据时段里
    assert res2.window_minutes == 1 and res2.m1.ts.tolist() == [0, 2 * M]
    path = tmp_path / "bad.yaml"
    path.write_text(
        '- date: 2026-09-21\n  start: "11:42"\n  end: "11:45"\n  symbols: all\n',
        encoding="utf-8",
    )
    (w,) = load_bad_windows(path)
    assert w.applies("KORU", date(2026, 9, 21)) and not w.applies("KORU", date(2026, 9, 22))
    a, z = w.ns_range()
    assert (z - a) == 3 * M


def test_resample_close_aligns_to_open():
    m1 = flat([100, 101, 102, 103, 104, 105, 106], 0)
    m1 = Bars(np.array([0, 1, 2, 3, 4, 5, 9]) * M, m1.open, m1.high, m1.low, m1.close, m1.volume,
              m1.vwap)  # fmt: skip
    b5 = resample_close(m1, 0, 5 * M)
    assert b5.ts.tolist() == [0, 5 * M] and b5.close.tolist() == [104, 106]


def _real_day_exists(symbol: str, day: str) -> bool:
    from trader.config import data_dir

    return (data_dir() / "bars" / "1s" / f"symbol={symbol}" / f"date={day}.parquet").exists()


@pytest.mark.skipif(not _real_day_exists("KORU", "2026-09-21"), reason="本地没有 KORU 实际数据")
def test_koru_2026_09_21_is_flagged_on_real_data():
    # 11:42–11:43 一串离群成交（约 21.5，而价格在 22.45 附近），造成几十次虚假摆动
    rep = build_report(["KORU"], "2026-09-21", "2026-09-21", benchmark_tickers=())
    a = rep.data_issues
    assert len(a) == 1 and "坏数据时段删除 3 分钟" in a.at[0, "data_reason"]
    # 清洗后摆动次数回到正常水平；不清洗时 11:42 那一串坏成交会造成十几次虚假摆动
    raw = build_report(
        ["KORU"], "2026-09-21", "2026-09-21", benchmark_tickers=(), bad_windows=(),
        print_filter_adr=float("inf"),
    )  # fmt: skip
    assert raw.daily["swing_n_k025"].iloc[0] - rep.daily["swing_n_k025"].iloc[0] >= 10


# ---------- 扩展时段 ----------


def _eth_frame() -> pl.DataFrame:
    rows = [
        # session, open, high, low, close, volume
        ("pre", 101.0, 101.5, 100.5, 101.2, 500.0),
        ("pre", 101.2, 120.0, 80.0, 101.3, 10.0),  # 成交量太小：不参与取价
        ("pre", 101.3, 102.0, 101.0, 101.8, 800.0),
        ("regular", 102.0, 103.0, 101.0, 102.5, 10000.0),
        ("post", 102.5, 102.6, 102.4, 102.45, 300.0),
        ("post", 102.45, 102.5, 102.0, 99.0, 5.0),  # 最后一根太小：最后一笔取前一根
    ]
    return pl.DataFrame(
        rows, schema=["session", "open", "high", "low", "close", "volume"], orient="row"
    )


def test_eth_small_bars_do_not_set_premarket_high_low():
    eth = EthDay.from_frame(_eth_frame(), 1.0, 100)
    assert eth.pm_high == 102.0 and eth.pm_low == 100.5
    assert eth.pre_first == 101.0 and eth.post_last == 102.45
    assert eth.vol_pre == 1310 and eth.vol_post == 305


def test_eth_gap_parts_sum_to_gap():
    eth = EthDay.from_frame(_eth_frame(), 1.0, 100)
    eth.prev_post_last = 100.4
    o, pc, adr = 102.0, 100.0, 2.0
    m = eth_metrics(eth, o, pc, adr)
    total = m["gap_post_adr"] + m["gap_overnight_adr"] + m["gap_pre_adr"]
    assert total == pytest.approx((o - pc) / adr)
    assert m["gap_post_adr"] == pytest.approx(0.2) and m["pm_range_adr"] == pytest.approx(0.75)
    eth.prev_post_last = math.nan  # 前一日盘后缺失：三段都为空
    assert all(math.isnan(m2) for m2 in list(eth_metrics(eth, o, pc, adr).values())[:3])


# ---------- 端到端 ----------


@pytest.fixture
def ds(tmp_path):
    days = [date(2026, 1, d) for d in (5, 6, 7, 8, 9)]
    return make_dataset(tmp_path, days, symbols=("AAA", "BBB", "CCC"))


def test_build_report_end_to_end(ds, tmp_path):
    rep = build_report(
        "AAA, BBB zzz",
        "2026-01-07",
        "2026-01-09",
        history=ds.history,
        benchmark_tickers=("CCC",),
        adr_days=2,
        leveraged_pairs={"AAA": ("BBB", 2), "XXX": ("AAA", 3)},
        compare_periods=("2026-01-08", "2026-01-09"),
    )
    assert rep.missing_symbols == ["ZZZ"]
    assert rep.tickers == ["AAA", "BBB"] and rep.benchmarks == ["CCC"]
    assert len(rep.daily) == 9 and bool(rep.daily["included"].all())
    assert rep.excluded.empty
    r = rep.ranking
    assert set(r.index) == {"AAA", "BBB", "CCC"}
    assert r["benchmark"].tolist().count(True) == 1
    for c in ("score_total", "score_activity", "score_rotation", "score_swing_value"):
        assert ((r[c] >= 0) & (r[c] <= 100)).all()
    assert any("证券数量较少" in n for n in rep.notes)
    assert any("XXX" in n for n in rep.notes)  # 杠杆对里缺数据的证券
    assert rep.bucket_rv_share.shape == (3, 13)
    assert np.allclose(rep.bucket_rv_share.sum(axis=1), 1, atol=0.3)  # 各桶中位数之和接近 1
    assert set(rep.sensitivity.columns.get_level_values(0)) == {"0.1", "0.15", "0.25"}
    assert set(rep.open_skip.columns.get_level_values(0)) == {"0", "5", "15", "30", "90"}
    assert "open_swing_n_mean" in r.columns and "conversion" in r.columns
    assert list(rep.leveraged.index) == ["AAA / BBB"]
    assert rep.compare is not None and len(rep.compare) == 3  # 含参照证券
    assert any("没有 SPY" in n for n in rep.notes)
    assert {False, True} == set(rep.swings["confirmed"])  # 未确认的最后一段单独一行
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "anomaly_days.csv").write_text("old", encoding="utf-8")
    out = rep.export(tmp_path / "out")
    assert not (out / "anomaly_days.csv").exists()  # 旧版本的文件被删掉
    names = {p.name for p in out.iterdir()}
    assert {
        "ranking.csv", "daily.csv", "swings.parquet", "excluded_days.csv", "data_issue_days.csv",
        "special_days.csv",
        "threshold_sensitivity.csv", "leveraged_pairs.csv", "extended_hours.csv",
        "period_compare.csv", "open_skip_ranking.csv",
    } <= names  # fmt: skip

    from trader.analytics import report

    report.styled_ranking(rep).to_html()
    report.styled_sensitivity(rep).to_html()
    report.styled_open_skip(rep).to_html()
    for fig in (
        report.fig_quadrant(rep),
        report.fig_boxplots(rep),
        report.fig_distributions(rep),
        report.fig_heatmap(rep),
        report.fig_detail_timeseries(rep, "AAA"),
        report.fig_detail_day(rep, "AAA"),
        report.fig_eth_scatter(rep)[0],
    ):
        assert fig.axes
    assert len(report.param_summary(rep)) > 5


def test_review_days_figures_and_exclusion(ds):
    # 每天都放一个坏数据时段、把密度阈值调到 0：两类日子都有，核对图能画，剔除开关只管数据异常
    windows = tuple(BadWindow(date(2026, 1, d), time(10, 0), time(10, 5)) for d in (7, 8, 9))
    rep = build_report(
        ["AAA"], "2026-01-07", "2026-01-09", history=ds.history, benchmark_tickers=(), adr_days=2,
        anomaly_swing_density=0.0, bad_windows=windows, exclude_data_issue_days=True,
    )  # fmt: skip
    from trader.analytics import report

    assert len(rep.data_issues) == 3 and len(rep.special_days) == 3
    assert set(rep.excluded["reason"]) == {"数据异常日（已设置剔除）"}
    assert rep.daily["included"].tolist() == [False] * 3
    assert rep.daily["window_minutes"].tolist() == [5.0] * 3
    for kind in ("data", "special"):
        figs = report.figs_review_days(rep, kind)
        assert len(figs) == 1 and len(figs[0].axes) == 4  # 3 张小图 + 1 个空位


def test_merge_periods_and_tradable_score(ds):
    kw = {"history": ds.history, "benchmark_tickers": (), "adr_days": 2}
    full = build_report(["AAA", "BBB", "CCC"], "2026-01-07", "2026-01-09", **kw)
    merged = build_report(
        ["AAA", "BBB", "CCC"], "2026-01-07", "2026-01-09",
        merge_periods=(("2026-01-07", "2026-01-07"), ("2026-01-09", "2026-01-09")), **kw,
    )  # fmt: skip
    assert set(merged.daily["date"]) == {date(2026, 1, 7), date(2026, 1, 9)}
    assert set(merged.swings["date"]) <= {date(2026, 1, 7), date(2026, 1, 9)}
    # 区间之间的 1 月 8 日照样读取，1 月 9 日的 ADR 和连续计算时一样
    a = full.daily.set_index(["symbol", "date"])["adr"]
    b = merged.daily.set_index(["symbol", "date"])["adr"]
    assert b.to_numpy() == pytest.approx(a.loc[b.index].to_numpy())
    assert any("合并区间" in n for n in merged.notes)
    r = full.ranking
    assert list(r.index) == list(r["score_tradable"].sort_values(ascending=False).index)
    # 每日转化效率中位数 = 每天 可获取波动 / ADR% 的中位数
    d = full.daily[full.daily["symbol"] == "AAA"]
    expect = (d["swing_capturable_pct_k025"] / d["adr_pct"]).median()
    assert r.at["AAA", "daily_conversion_median"] == pytest.approx(expect)


def test_adr_shortfall_is_excluded(ds):
    rep = build_report(
        ["AAA"], "2026-01-05", "2026-01-09", history=ds.history, benchmark_tickers=(), adr_days=2,
    )  # fmt: skip
    # 前两天之前不足 2 个交易日：ADR 为空、不参与汇总，列在 excluded 里
    ex = rep.excluded
    assert list(ex["date"]) == [date(2026, 1, 5), date(2026, 1, 6)]
    assert set(ex["reason"]) == {"ADR 数据不足"}
    assert rep.ranking.loc["AAA", "days"] == 3


def test_score_k_must_be_a_threshold(ds):
    with pytest.raises(ValueError, match="score_k"):
        build_report(["AAA"], "2026-01-07", "2026-01-09", history=ds.history, score_k=0.3)
