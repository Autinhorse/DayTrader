"""日内波动报表（trader.analytics，规范第 8 节）：摆动算法、效率比、ATR、缺失分钟、端到端。"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date

import matplotlib
import numpy as np
import pytest

from conftest import make_dataset
from trader.analytics import ReportParams, build_report
from trader.analytics.daily_metrics import Bars, DayInput, compute_day, efficiency, vwap_crosses
from trader.analytics.loader import atr_wilder
from trader.analytics.swings import find_swings

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


# ---------- 摆动算法 ----------


def test_straight_line_has_no_complete_swing():
    sw, amb = find_swings(*point_bars([100 + 0.1 * i for i in range(390)]), theta=1.0)
    assert sw == [] and amb == 0


def test_sine_wave_swings():
    # 周期 40 分钟、振幅 2，4 个周期：极值在第 10、30、…、150 分钟；第 160 分钟回到 100
    prices = [100 + 2 * math.sin(2 * math.pi * t / 40) for t in range(161)]
    sw, _ = find_swings(*point_bars(prices), theta=1.0)
    assert len(sw) == 8  # 开盘 → 第一个高点，之后 7 段完整摆动；最后一段未确认不计
    assert sw[0].start_price == pytest.approx(100) and sw[0].amplitude == pytest.approx(2)
    assert all(s.amplitude == pytest.approx(4) for s in sw[1:])
    assert [s.direction for s in sw[:3]] == [1, -1, 1]
    assert sw[1].start_ts == 10 * M and sw[1].end_ts == 30 * M
    assert sw[1].confirm_ts > sw[1].end_ts


def test_random_walk_swings_alternate_and_exceed_threshold():
    rnd = np.random.default_rng(7)
    prices = list(100 + np.cumsum(rnd.normal(0, 0.1, 390)))
    sw, _ = find_swings(*point_bars(prices), theta=0.5)
    assert len(sw) > 3
    assert all(s.amplitude >= 0.5 - 1e-9 for s in sw)
    assert all(a.direction == -b.direction for a, b in zip(sw, sw[1:], strict=False))
    assert all(a.end_ts == b.start_ts for a, b in zip(sw, sw[1:], strict=False))  # 首尾相接


def test_first_swing_starts_from_low_when_open_dips_first():
    # ATR 10，阈值 0.25 ATR = 2.5：开盘 100，先跌 0.2 ATR 到 98，再涨到 105，回落到 101 确认
    prices = [100, 99, 98, 100, 102, 105, 103, 101]
    sw, _ = find_swings(*point_bars(prices), theta=2.5)
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
    up_first, amb1 = find_swings(ts, o, h, lo, 2.0, fine=_fine("high_first"))
    down_first, amb2 = find_swings(ts, o, h, lo, 2.0, fine=_fine("low_first"))
    assert amb1 == 0 and amb2 == 0
    # 先涨：开盘区间低点 99.5 → 103 确认，再从 99 反弹 2 确认 103 → 99
    assert [(s.start_price, s.end_price) for s in up_first] == [(99.5, 103), (103, 99)]
    # 先跌：方向未定时到 99，再涨到 103 确定向上；第 3 分钟回落 2.5 确认 99 → 103
    assert [(s.start_price, s.end_price) for s in down_first] == [(99, 103)]


def test_same_minute_conflict_without_1s_bars_is_conservative():
    ts, o, h, lo = _conflict_minute()
    sw, amb = find_swings(ts, o, h, lo, 2.0, fine=None)
    assert amb == 1
    assert [(s.start_price, s.end_price) for s in sw] == [(99, 103)]  # 取当分钟确认较少的顺序


# ---------- 其他指标 ----------


def test_efficiency_line_and_round_trip():
    assert efficiency(100, np.array([101, 102, 103, 104.0])) == pytest.approx(1)
    back = np.array([101, 102, 101, 100, 101, 102, 101, 100.0])
    assert efficiency(100, back) == pytest.approx(0)


def test_atr_wilder_and_no_lookahead():
    h = np.array([11, 12, 13, 12, 15.0])
    lo = np.array([9, 10, 11, 10, 11.0])
    c = np.array([10, 11, 12, 11, 14.0])
    atr = atr_wilder(h, lo, c, 3)
    # TR = 2, 2, 2, 2(=12−10), 4(=15−11)；初始值 2，之后 (2×2+2)/3 = 2，(2×2+4)/3
    assert np.isnan(atr[:2]).all()
    assert atr[2] == pytest.approx(2) and atr[3] == pytest.approx(2)
    assert atr[4] == pytest.approx(8 / 3)
    # 改动最后一天不影响之前的值
    h2 = h.copy()
    h2[-1] = 50
    assert np.array_equal(atr_wilder(h2, lo, c, 3)[:4], atr[:4], equal_nan=True)


def test_true_range_uses_previous_close():
    atr = atr_wilder(np.array([10, 20.0]), np.array([9, 19.0]), np.array([10, 19.5]), 1)
    assert atr[1] == pytest.approx(20 - 10)  # 跳空：TR = max(H, PC) − min(L, PC)


def test_missing_minutes_not_filled():
    open_ = 14 * 3600 * 10**9
    rows = [(100, 100, 100, 100), (100, 101, 100, 101), (101, 102, 101, 102)]
    m1 = bars(rows, open_)
    m1 = Bars(
        ts=np.array([0, 1, 9]) * M + open_,  # 第 2–8 分钟没有成交
        open=m1.open, high=m1.high, low=m1.low, close=m1.close, volume=m1.volume, vwap=m1.vwap,
    )  # fmt: skip
    inp = DayInput(open_, open_ + 10 * M, m1, m1, atr=4.0, pc=99.0)
    met = compute_day(inp, ReportParams(swing_thresholds_atr=(0.25,))).metrics
    r = np.diff(np.log([100, 101, 102]))
    assert met["rv_5min_pct"] == pytest.approx(math.sqrt((r**2).sum()) * 100)  # 没有填充出来的 0
    assert met["missing_ratio"] == pytest.approx(0.7)
    assert met["max_gap_min"] == 7
    assert met["gap_atr"] == pytest.approx(0.25)


def test_vwap_crosses_need_hold():
    # VWAP 固定 100（vwap 列是 100、成交量相同）：收盘 101,99,99,99,101,99,99,99
    closes = [101, 99, 99, 99, 101, 99, 99, 99, 99]
    m1 = bars([(c, c, c, c) for c in closes])
    vwap = np.full(len(closes), 100.0)
    assert vwap_crosses(m1, vwap, 3, len(closes) * M) == 1  # 第 5 分钟回到上方只停留 1 分钟，不算
    assert vwap_crosses(m1, vwap, 1, len(closes) * M) == 3


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
        atr_days=2,
        atr_warmup_factor=1,
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
    assert rep.bucket_rv_share.shape == (3, 13)
    assert np.allclose(rep.bucket_rv_share.sum(axis=1), 1, atol=0.3)  # 各桶中位数之和接近 1
    out = rep.export(tmp_path / "out")
    assert {"ranking.csv", "daily.csv", "swings.parquet", "excluded_days.csv"} <= {
        p.name for p in out.iterdir()
    }

    from trader.analytics import report

    report.styled_ranking(rep).to_html()
    for fig in (
        report.fig_quadrant(rep),
        report.fig_boxplots(rep),
        report.fig_distributions(rep),
        report.fig_heatmap(rep),
        report.fig_detail_timeseries(rep, "AAA"),
        report.fig_detail_day(rep, "AAA"),
    ):
        assert fig.axes
    assert len(report.param_summary(rep)) > 5


def test_atr_warmup_shortfall_is_excluded(ds):
    rep = build_report(
        ["AAA"], "2026-01-05", "2026-01-09", history=ds.history, benchmark_tickers=(), atr_days=2,
        atr_warmup_factor=1,
    )  # fmt: skip
    # 前两天之前不足 2 个交易日：ATR 为空、不参与汇总，列在 excluded 里
    ex = rep.excluded
    assert list(ex["date"]) == [date(2026, 1, 5), date(2026, 1, 6)]
    assert set(ex["reason"]) == {"ATR 预热数据不足"}
    assert rep.ranking.loc["AAA", "days"] == 3


def test_score_k_must_be_a_threshold(ds):
    with pytest.raises(ValueError, match="score_k"):
        build_report(["AAA"], "2026-01-07", "2026-01-09", history=ds.history, score_k=0.3)
