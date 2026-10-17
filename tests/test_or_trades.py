"""开盘区间交易快速模拟（决策 0016）：用手工构造的 1 分钟 bar
核对入场、止损、目标、收盘平仓和成本。"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from trader.analytics import or_trades as T

# 开盘区间（前 60 分钟）99 ~ 101，中线 100；ADR = 2；之后默认每根 bar 都是 100.5
ADR = 2.0


def _day(changes: dict[int, tuple[float, float, float, float]], cost: float = 0.0,
         day: date = date(2026, 1, 5)) -> T.MinuteDay:  # fmt: skip
    n = 390
    px = np.full((4, n), 100.5)
    px[:, :60] = 100.0
    px[1, 0], px[2, 1] = 101.0, 99.0
    for i, ohlc in changes.items():
        px[:, i] = ohlc
    return T.MinuteDay("AAA", day, np.arange(n, dtype=np.int16), *px, adr=ADR, cost_pct=cost)


def _one(d: T.MinuteDay, rule: T.Rule) -> dict:
    t = T.simulate([d], rule)
    assert len(t) == 1
    return t.iloc[0].to_dict()


POKE = {70: (100.6, 101.2, 100.6, 101.1)}  # 第 70 分钟碰到上沿（101.2 > 101），收盘仍在上沿之外
DOWN = {80: (100.5, 100.5, 99.9, 100.2)}  # 第 80 分钟回落到中线以下


def test_fade_target_mid():
    t = _one(_day(POKE | DOWN), T.Rule("fade", slip_adr=0))
    assert t["side"] == -1 and t["entry_min"] == 70 and t["entry"] == pytest.approx(101.0)
    assert t["reason"] == "目标" and t["exit"] == pytest.approx(100.0) and t["exit_min"] == 80
    assert t["risk_adr"] == pytest.approx(0.15)  # 止损 101 + 0.15 × 2 = 101.3
    assert t["ret_adr"] == pytest.approx(0.5) and t["r"] == pytest.approx(1.0 / 0.3)
    assert t["or_width"] == pytest.approx(1.0)


def test_fade_hold_to_close_and_cost():
    t = _one(_day(POKE | DOWN, cost=0.05), T.Rule("fade", target="opposite", slip_adr=0))
    assert t["reason"] == "收盘" and t["exit"] == pytest.approx(100.5) and t["exit_min"] == 385
    cost = 0.0005 * (101.0 + 100.5)
    assert t["ret_adr"] == pytest.approx((0.5 - cost) / ADR)
    assert t["ret_pct"] == pytest.approx((0.5 - cost) / 101.0 * 100)


def test_fade_stopped_in_entry_bar():
    t = _one(_day({70: (100.6, 101.4, 100.6, 101.35)} | DOWN), T.Rule("fade", slip_adr=0))
    assert t["reason"] == "止损" and t["exit"] == pytest.approx(101.3) and t["exit_min"] == 70


def test_stop_wins_when_both_hit_in_one_bar():
    both = {75: (100.9, 101.35, 99.9, 100.0)}  # 同一根 bar 既到止损又到目标：按止损
    t = _one(_day(POKE | both), T.Rule("fade", slip_adr=0))
    assert t["reason"] == "止损" and t["exit_min"] == 75


def test_reclaim():
    back = {71: (101.1, 101.15, 100.7, 100.8)}  # 收回区间里
    t = _one(_day(POKE | back | DOWN), T.Rule("reclaim", slip_adr=0))
    assert t["side"] == -1 and t["entry_min"] == 71 and t["entry"] == pytest.approx(100.8)
    assert t["risk_adr"] == pytest.approx((101.5 - 100.8) / ADR)  # 突破以来最高 101.2 + 0.3
    assert t["reason"] == "目标" and t["exit"] == pytest.approx(100.0)
    # 收回以前先碰到另一边：不做
    other = {71: (101.0, 101.0, 98.5, 100.8)}
    assert T.simulate([_day(POKE | other)], T.Rule("reclaim")).empty


def test_breakout_with_slippage():
    t = _one(_day(POKE | DOWN), T.Rule("breakout"))
    slip = 0.01 * ADR
    assert t["side"] == 1 and t["entry"] == pytest.approx(101.0 + slip)
    assert t["reason"] == "止损" and t["exit"] == pytest.approx(100.0 - slip)  # 止损在中线


def test_no_entry_after_cutoff_and_quarter_summary():
    late = {340: (100.6, 101.2, 100.6, 101.1)}
    assert T.simulate([_day(late)], T.Rule("fade")).empty
    days = [_day(POKE | DOWN, day=date(2026, 1, 5)), _day(POKE | DOWN, day=date(2026, 4, 6))]
    s = T.summarize(T.simulate(days, T.Rule("fade", slip_adr=0)))
    assert list(s.index) == ["2026Q1", "2026Q2", "全部"]
    assert s.loc["全部", "trades"] == 2 and s.loc["全部", "win"] == 1.0
    with pytest.raises(ValueError):
        T.Rule("other")
