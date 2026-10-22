"""Volume Profile 研究模块（trader.analytics.volume_profile，规范第 7 节）：用合成的 1 秒 bar
检查守恒、时点、细网格合并、复权、竞价剔除和清洗。"""

from __future__ import annotations

import random
from datetime import date, time
from pathlib import Path

import numpy as np
import polars as pl
import pytest
from tests.conftest import CAL, make_dataset

from trader.analytics.cleaning import BadWindow
from trader.analytics.volume_profile import ProfileParams, VolumeProfile
from trader.core import volume_profile as vp
from trader.core.timeutil import NS_PER_SEC, ny_to_ns
from trader.data.catalog import Catalog
from trader.data.corporate_actions import ACTION_SCHEMA, actions_path
from trader.data.history import HistoryService
from trader.data.store import BAR_SCHEMA, BarStore, fingerprint
from trader.data.validate import validate_day

DAYS = [date(2026, 1, d) for d in (5, 6, 7, 8, 9, 12, 13, 14)]
P = ProfileParams(bad_windows=())


@pytest.fixture(scope="module")
def ds(tmp_path_factory):
    return make_dataset(tmp_path_factory.mktemp("vp"), DAYS)


def _vp(ds, p: ProfileParams = P) -> VolumeProfile:
    return VolumeProfile(p, HistoryService(ds.root, CAL, Catalog(ds.root / "catalog.sqlite")))


def _raw(ds, day: date) -> pl.DataFrame:
    return BarStore(ds.root).read_day("AAA", day)


def test_conservation(ds):
    v = _vp(ds)
    for d in DAYS:
        g = v.store.day("AAA", CAL.trading_day(d))
        assert g is not None
        total = sum(fp.total for fp in g.grids.values())
        assert total == pytest.approx(_raw(ds, d)["volume"].sum())
        for seg, fp in g.grids.items():
            assert fp.total == pytest.approx(g.stats.volume_in[seg] - g.stats.volume_small[seg])
    # 竞价单独记账：9:30:00 和 15:59:59 各一秒
    g = v.store.day("AAA", CAL.trading_day(DAYS[0]))
    assert g is not None and set(g.grids) == {"open_cross", "rth", "close_cross"}


def test_cache_roundtrip(ds):
    td = CAL.trading_day(DAYS[2])
    a = _vp(ds).store.day("AAA", td)
    b = _vp(ds).store.day("AAA", td)  # 新实例：读文件
    assert a is not None and b is not None and a.stats == b.stats
    for k in a.grids:
        np.testing.assert_allclose(merge_vol(a.grids[k], b.grids[k]), 0, atol=1e-6)


def merge_vol(x: vp.FineProfile, y: vp.FineProfile) -> np.ndarray:
    return vp.merge([x, y], [1.0, -1.0]).vol


def test_composite_equals_direct_from_bars(ds):
    """5 个 daily 细网格相加 = 直接用 5 天的 bar 计算（没有清洗改动、没有拆股时）。"""
    v = _vp(ds)
    w = v.window("AAA", DAYS[-1], "composite", n=5)
    assert w.days == tuple(DAYS[-6:-1])
    frames = [_raw(ds, d) for d in DAYS[-6:-1]]
    df = pl.concat(frames)
    keep = []
    for d, f in zip(DAYS[-6:-1], frames, strict=True):
        td = CAL.trading_day(d)
        ts = f["ts_start"].to_numpy()
        keep.append((ts > td.open) & (ts < td.close - NS_PER_SEC))  # 默认剔除收盘竞价、保留开盘
        keep[-1] |= ts == td.open
    m = np.concatenate(keep)
    direct = vp.distribute(
        df["high"].to_numpy()[m], df["low"].to_numpy()[m], df["close"].to_numpy()[m],
        df["volume"].to_numpy()[m],
    )  # fmt: skip
    np.testing.assert_allclose(merge_vol(w.fine, direct), 0, atol=1e-6)


def test_value_area_on_real_window(ds):
    lv = _vp(ds).get_levels("AAA", DAYS[-1], "composite", n=5)
    assert lv["val"] <= lv["poc"] <= lv["vah"]
    assert lv["profile_low"] <= lv["val"] and lv["vah"] <= lv["profile_high"]


def _truncated(tmp: Path, cut_day: date, t: int) -> HistoryService:
    """复制数据集：cut_day 只保留 t 之前已收盘的 1 秒 bar，之后的日子删除。"""
    src = make_dataset(tmp / "full", DAYS)
    store, cat = BarStore(tmp / "cut"), Catalog(tmp / "cut" / "catalog.sqlite")
    for d in DAYS:
        if d > cut_day:
            break
        df = BarStore(src.root).read_day("AAA", d)
        if d == cut_day:
            df = df.filter(pl.col("ts_start") + NS_PER_SEC <= t)
        store.write_day("AAA", d, df)
        cat.record("AAA", d, validate_day(df, CAL.trading_day(d)), fingerprint(df), 0)
    return HistoryService(tmp / "cut", CAL, cat)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_point_in_time(tmp_path, ds, seed):
    """删掉 t 之后的全部数据重算，developing、前一日、复合 Profile 完全相同（规范 7）。"""
    rnd = random.Random(seed)
    day = DAYS[rnd.randrange(5, len(DAYS) - 1)]
    t = ny_to_ns(day, time(rnd.randrange(9, 15), rnd.randrange(60), rnd.randrange(60)))
    t = max(t, CAL.trading_day(day).open + 60 * NS_PER_SEC)
    # 离群阈值设得很小，确保清洗真的改动了价格（检查清洗也只用 t 之前的数据）
    p = P.with_(print_filter_adr=0.002)
    full = _vp(ds, p)
    cut = VolumeProfile(p, _truncated(tmp_path, day, t))
    assert full.store.day("AAA", CAL.trading_day(day)).stats.fixed_prints > 0  # type: ignore[union-attr]
    a = full.get_developing("AAA", day, until=t)
    b = cut.get_developing("AAA", day, until=t)
    pd_cols = ["dpoc", "dvah", "dval", "cum_volume"]
    np.testing.assert_allclose(a[pd_cols].to_numpy(), b[pd_cols].to_numpy(), rtol=1e-12)
    wa = full.window("AAA", day, "developing", until=t)
    wb = cut.window("AAA", day, "developing", until=t)
    np.testing.assert_allclose(merge_vol(wa.fine, wb.fine), 0, atol=1e-6)
    for kind, kw in [("daily", {}), ("composite", {"n": 3}), ("decay", {"half_life": 3})]:
        la, lb = full.get_levels("AAA", day, kind, **kw), cut.get_levels("AAA", day, kind, **kw)
        for k in ("poc", "vah", "val", "total_volume", "bin_size", "adr"):
            assert la[k] == pytest.approx(lb[k], rel=1e-12), (kind, k)


def test_incremental_developing_matches_window(ds):
    v = _vp(ds, P.with_(print_filter_adr=0.002))
    day = DAYS[6]
    dev = v.get_developing("AAA", day)
    for minute in (5, 47, 200, 390):
        t = CAL.trading_day(day).open + minute * 60 * NS_PER_SEC
        lv = v.levels_of(v.window("AAA", day, "developing", until=t))
        row = dev.iloc[minute - 1]
        assert (row.dpoc, row.dvah, row.dval) == pytest.approx((lv["poc"], lv["vah"], lv["val"]))
        assert row.cum_volume == pytest.approx(lv["total_volume"])
    assert not dev.iloc[13].stable and dev.iloc[14].stable


def _add_rows(root: Path, day: date, rows: list[tuple]) -> None:
    store, cat = BarStore(root), Catalog(root / "catalog.sqlite")
    df = pl.concat(
        [store.read_day("AAA", day), pl.DataFrame(rows, schema=BAR_SCHEMA, orient="row")]
    )
    df = df.sort("ts_start")
    store.write_day("AAA", day, df)
    cat.record("AAA", day, validate_day(df, CAL.trading_day(day)), fingerprint(df), 0)


def test_close_cross_exclusion(tmp_path):
    d = make_dataset(tmp_path, DAYS[:2])
    td = CAL.trading_day(DAYS[1])
    rows = [
        (td.close, 100.0, 100.0, 100.0, 100.0, 50_000.0, 100.0, 1),
        (td.close + 5 * NS_PER_SEC, 100.0, 100.0, 100.0, 100.0, 7.0, 100.0, 1),
    ]
    _add_rows(d.root, DAYS[1], rows)
    hist = HistoryService(d.root, CAL, Catalog(d.root / "catalog.sqlite"))
    a = VolumeProfile(P, hist).get_profile("AAA", DAYS[1], DAYS[1], bin_size=0.01)
    b = VolumeProfile(P.with_(exclude_close_cross=False), hist).get_profile(
        "AAA", DAYS[1], DAYS[1], bin_size=0.01
    )
    raw = BarStore(d.root).read_day("AAA", DAYS[1])
    cross = raw.filter(pl.col("ts_start").is_between(td.close - NS_PER_SEC, td.close))
    assert b.fine.total - a.fine.total == pytest.approx(cross["volume"].sum())
    # 16:00:05 是盘后，成交量小于 100 不参与
    post = VolumeProfile(P, hist).store.day("AAA", td)
    assert post is not None and post.stats.volume_small["post"] == pytest.approx(7.0)


def test_bad_window_removed(ds):
    day = DAYS[3]
    w = BadWindow(day, time(11, 42), time(11, 45))
    a = _vp(ds).store.day("AAA", CAL.trading_day(day))
    b = _vp(ds, P.with_(bad_windows=(w,))).store.day("AAA", CAL.trading_day(day))
    raw = _raw(ds, day)
    lo, hi = w.ns_range()
    inside = raw.filter(pl.col("ts_start").is_between(lo, hi - 1))["volume"].sum()
    assert a is not None and b is not None and inside > 0
    assert a.grids["rth"].total - b.grids["rth"].total == pytest.approx(inside)
    assert b.stats.window_bars == 180


def test_split_continuity(tmp_path):
    """1 拆 2：拆股前的日子按 T 的口径换算，复合 Profile 的价格连续、股数翻倍。"""
    d = make_dataset(tmp_path, DAYS)
    split_day = DAYS[4]
    store, cat = BarStore(d.root), Catalog(d.root / "catalog.sqlite")
    for day in DAYS[:4]:  # 拆股前价格翻倍、股数减半
        df = store.read_day("AAA", day).with_columns(
            [pl.col(c) * 2 for c in ("open", "high", "low", "close", "vwap")]
            + [pl.col("volume") / 2]
        )
        store.write_day("AAA", day, df)
        cat.record("AAA", day, validate_day(df, CAL.trading_day(day)), fingerprint(df), 0)
    pl.DataFrame(
        [("AAA", "split", split_day, 2.0, None)], schema=ACTION_SCHEMA, orient="row"
    ).write_parquet(actions_path(d.root))
    hist = HistoryService(d.root, CAL, Catalog(d.root / "catalog.sqlite"))
    adj = VolumeProfile(P, hist).window("AAA", DAYS[6], "composite", n=5)
    ref = VolumeProfile(P, make_dataset(tmp_path / "plain", DAYS).history).window(
        "AAA", DAYS[6], "composite", n=5
    )
    assert adj.fine.extent() == ref.fine.extent()
    assert adj.fine.total == pytest.approx(ref.fine.total)
    a = VolumeProfile(P, hist).get_levels("AAA", DAYS[6], "composite", n=5)
    r = VolumeProfile(P, make_dataset(tmp_path / "plain2", DAYS).history).get_levels(
        "AAA", DAYS[6], "composite", n=5
    )
    for k in ("poc", "vah", "val", "adr"):
        assert a[k] == pytest.approx(r[k], abs=0.011), k


def test_levels_table_and_naked(ds):
    v = _vp(ds)
    tab = v.levels_table("AAA", DAYS[5], DAYS[6])
    assert len(tab) == 2 * len(v.default_windows())
    pre = tab["profile_type"] == "premarket"
    assert (tab.loc[pre, "as_of"].dt.time == time(9, 30)).all()  # 截至开盘
    assert (tab.loc[~pre, "as_of"].dt.time == time(4, 0)).all()  # 只用前一日及之前
    nk = v.naked_pocs("AAA", DAYS[0], DAYS[-1])
    assert list(nk["poc_date"]) == DAYS
    for poc_day, touched in zip(nk["poc_date"], nk["touched_date"], strict=True):
        if touched is not None:
            assert touched > poc_day
    at = v.naked_at("AAA", DAYS[-1])
    assert set(at["poc_date"]) <= set(DAYS[:-1])


@pytest.mark.skipif(
    not Path("data/bars/1s/symbol=KORU/date=2026-07-15.parquet").exists(), reason="没有 KORU 数据"
)
def test_koru_split_real_data():
    """KORU 2026-07-15 1 拆 20：跨拆股的 5 日复合 Profile 在拆股后的价格区间内连续。"""
    v = VolumeProfile()
    w = v.window("KORU", "2026-07-17", "composite", n=5)
    lo, hi = w.fine.extent()  # type: ignore[misc]
    assert lo / 100 > 10 and hi / 100 < 40  # 拆股前价格在 400 以上，未换算会落在这个区间之外
    lv = v.get_levels("KORU", "2026-07-17", "composite", n=5)
    assert lv["val"] <= lv["poc"] <= lv["vah"] and 0.5 < lv["va_width_adr"] < 5
