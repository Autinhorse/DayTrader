"""K 线图上的成交量分布（gui.volume_overlay）和它用到的 VolumeProfile.between / binned：
任意时间段的分布与已有窗口一致、回放中只用回放时钟之前的数据、各模式的数据结构。"""

from __future__ import annotations

from datetime import time

import numpy as np
import pytest
from tests.conftest import CAL, make_dataset
from tests.test_volume_profile import DAYS, P, _truncated, _vp, merge_vol

from trader.analytics.volume_profile import VolumeProfile
from trader.core.timeutil import NS_PER_SEC, ny_to_ns
from trader.gui.services import from_ny_seconds, ny_seconds
from trader.gui.volume_overlay import profile_payload


@pytest.fixture(scope="module")
def ds(tmp_path_factory):
    return make_dataset(tmp_path_factory.mktemp("vpo"), DAYS)


def test_between_matches_windows(ds):
    v = _vp(ds, P.with_(print_filter_adr=0.002))
    day = DAYS[5]
    td = CAL.trading_day(day)
    # 整天：等于下一天的“前一日”窗口
    whole = v.between("AAA", td.start, td.end)
    prev = v.window("AAA", DAYS[6], "daily")
    np.testing.assert_allclose(merge_vol(whole.fine, prev.fine), 0, atol=1e-6)
    # 当天开盘到 t：等于 developing
    t = ny_to_ns(day, time(11, 17, 23))
    part = v.between("AAA", td.start, t)
    dev = v.window("AAA", day, "developing", until=t)
    np.testing.assert_allclose(merge_vol(part.fine, dev.fine), 0, atol=1e-6)
    # 跨三天 = 三个整天相加
    tds = [CAL.trading_day(d) for d in DAYS[2:5]]
    span = v.between("AAA", tds[0].start, tds[-1].end)
    parts = [v.between("AAA", d.start, d.end).fine for d in tds]
    assert span.days == tuple(DAYS[2:5])
    assert span.fine.total == pytest.approx(sum(f.total for f in parts))
    # 分格：成交量守恒，关键价位与 levels_of 相同
    r = v.binned(dev)
    lv = v.levels_of(dev)
    assert r.bins["volume"].sum() == pytest.approx(dev.fine.total)
    assert r.levels is not None
    assert (r.levels.poc, r.levels.vah, r.levels.val) == pytest.approx(
        (lv["poc"], lv["vah"], lv["val"])
    )


@pytest.mark.parametrize("mode", ["today", "daily", "visible"])
def test_payload_point_in_time(tmp_path, ds, mode):
    """回放时钟 t：删掉 t 之后的全部数据重算，结果完全相同。"""
    day = DAYS[6]
    t = ny_to_ns(day, time(12, 3, 7))
    p = P.with_(print_filter_adr=0.002)
    full, cut = _vp(ds, p), VolumeProfile(p, _truncated(tmp_path, day, t))
    days = [CAL.trading_day(d) for d in DAYS[4:7]]
    vis = (days[0].start, CAL.trading_day(DAYS[-1]).end)  # 可见范围超过回放时钟也只用到 t
    a = profile_payload(full, "AAA", mode, days, until=t, visible=vis)
    b = profile_payload(cut, "AAA", mode, days, until=t, visible=vis)
    assert a == b and a["profiles"]


def test_payload_modes(ds):
    v = _vp(ds)
    days = [CAL.trading_day(d) for d in DAYS[3:]]
    assert profile_payload(v, "AAA", "off", days)["profiles"] == []
    assert profile_payload(v, "AAA", "visible", days)["profiles"] == []  # 等页面报告可见范围

    today = profile_payload(v, "AAA", "today", days)["profiles"]
    assert len(today) == 1 and today[0]["t0"] is None
    p = today[0]
    lv = v.levels_of(v.between("AAA", days[-1].start, days[-1].end))
    assert (p["poc"], p["vah"], p["val"]) == pytest.approx((lv["poc"], lv["vah"], lv["val"]))
    assert sum(p["vol"]) == pytest.approx(lv["total_volume"], abs=len(p["vol"]))
    lo, hi = p["va"]
    poc_low = p["low"] + p["poc_i"] * p["size"]
    assert lo <= p["poc_i"] <= hi and poc_low <= p["poc"] <= poc_low + p["size"]
    assert p["vol"][0] > 0 and p["vol"][-1] > 0  # 两端没有空格子

    c5 = profile_payload(v, "AAA", "c5", days)["profiles"][0]
    assert c5["n_days"] == 5 and c5["label"].startswith("前5日合并")
    prev = profile_payload(v, "AAA", "prev", days)["profiles"][0]
    assert prev["poc"] == pytest.approx(v.get_levels("AAA", DAYS[-1])["poc"])

    daily = profile_payload(v, "AAA", "daily", days)["profiles"]
    assert [(x["t0"], x["t1"]) for x in daily] == [
        (ny_seconds(d.start), ny_seconds(d.end)) for d in days
    ]

    vis = profile_payload(v, "AAA", "visible", days, visible=(days[1].open, days[2].close))
    assert vis["profiles"][0]["n_days"] == 2


def test_from_ny_seconds_roundtrip():
    for d in (DAYS[0], CAL.trading_day(DAYS[0]).day.replace(month=7)):
        ns = ny_to_ns(d, time(9, 30)) + 17 * NS_PER_SEC
        assert from_ny_seconds(ny_seconds(ns)) == ns
