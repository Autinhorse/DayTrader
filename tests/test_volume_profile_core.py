"""Volume Profile 纯计算部分（trader.core.volume_profile，规范第 2、3、7 节）。"""

from __future__ import annotations

import numpy as np
import pytest

from trader.core import volume_profile as vp


def _one(price: float, vol: float) -> vp.FineProfile:
    a = np.array([price])
    return vp.distribute(a, a, a, np.array([vol]))


def test_uniform_conserves_and_spreads():
    fp = vp.distribute(
        high=np.array([100.04, 100.00, 99.99]),
        low=np.array([100.00, 100.00, 99.97]),
        close=np.array([100.02, 100.00, 99.98]),
        volume=np.array([500.0, 70.0, 30.0]),
    )
    assert fp.total == pytest.approx(600.0)
    assert fp.base == 9997
    np.testing.assert_allclose(fp.vol, [10, 10, 10, 100 + 70, 100, 100, 100, 100])


@pytest.mark.parametrize("method", ["close", "typical", "vwap"])
def test_point_methods(method):
    fp = vp.distribute(
        np.array([10.10]), np.array([10.00]), np.array([10.04]), np.array([9.0]),
        vwap=np.array([10.07]), method=method,
    )  # fmt: skip
    expect = {"close": 1004, "typical": 1005, "vwap": 1007}[method]
    assert fp.extent() == (expect, expect) and fp.total == pytest.approx(9.0)


def test_vwap_missing_falls_back_to_typical():
    fp = vp.distribute(
        np.array([10.10]), np.array([10.00]), np.array([10.04]), np.array([9.0]),
        vwap=np.array([np.nan]), method="vwap",
    )  # fmt: skip
    assert fp.extent() == (1005, 1005)


def test_merge_and_weights():
    a, b = _one(10.00, 5), _one(10.03, 7)
    m = vp.merge([a, b], [1.0, 0.5])
    assert m.base == 1000 and m.total == pytest.approx(8.5)
    np.testing.assert_allclose(m.vol, [5, 0, 0, 3.5])


def test_split_adjusted_conserves_shares_value():
    # 1 拆 20：$400.00 → $20.00，成交量 × 20
    fp = _one(400.00, 10) + _one(400.20, 10)
    adj = fp.split_adjusted(20.0)
    assert adj.total == pytest.approx(400.0)
    assert adj.extent() == (2000, 2001)
    # 10 合 1：$1.00 → $10.00，一个价位分成 10 个
    rev = _one(1.00, 100).split_adjusted(0.1)
    assert rev.total == pytest.approx(10.0)
    assert rev.extent() == (995, 1004)


def test_bin_cents():
    assert vp.bin_cents("adr", adr=5.0) == 10
    assert vp.bin_cents("adr", adr=5.01) == 11  # 向上取整
    assert vp.bin_cents("pct", price=100.0) == 5
    assert vp.bin_cents("fixed", fixed=0.05) == 5
    assert vp.bin_cents("adr", adr=float("nan")) == 1


def test_rebin_alignment():
    fp = _one(10.03, 1) + _one(10.07, 2) + _one(10.10, 4)
    b = vp.rebin(fp, 5)
    assert b.first == 200  # 10.00–10.04
    np.testing.assert_allclose(b.vol, [1, 2, 4])
    assert b.low(0) == pytest.approx(10.00) and b.high(0) == pytest.approx(10.04)


def _normal(mu=100.0, sd=0.5, n=200_000, seed=1) -> vp.FineProfile:
    x = np.random.default_rng(seed).normal(mu, sd, n)
    return vp.distribute(x, x, x, np.ones(n))


def test_unimodal_normal():
    lv = vp.levels(_normal(), 5)
    assert lv is not None
    assert lv.poc == pytest.approx(100.0, abs=0.06)
    # 70% 价值区约为 ±1.04 个标准差
    assert lv.vah == pytest.approx(100.52, abs=0.08) and lv.val == pytest.approx(99.48, abs=0.08)
    assert lv.va_volume_frac >= 0.7
    assert lv.n_hvn == 1 and lv.lvn == ()
    assert abs(lv.volume_skew) < 0.05 and lv.poc_position == pytest.approx(0.5, abs=0.05)


def test_bimodal():
    fp = _normal(99.0, 0.3, seed=1) + _normal(101.0, 0.3, seed=2)
    lv = vp.levels(fp, 5)
    assert lv is not None and lv.n_hvn == 2
    assert lv.hvn[0] == pytest.approx(99.0, abs=0.1) and lv.hvn[1] == pytest.approx(101.0, abs=0.1)
    assert len(lv.lvn) == 1 and lv.lvn[0] == pytest.approx(100.0, abs=0.15)
    assert lv.lvn_significance[0] < 0.3


def test_single_price():
    lv = vp.levels(_one(50.37, 1000), 10)
    assert lv is not None
    assert lv.poc == lv.vah == lv.val == pytest.approx(50.37)
    assert lv.va_volume_frac == 1.0


def test_value_area_contiguous_and_minimal_pairs():
    rng = np.random.default_rng(3)
    for _ in range(200):
        vol = rng.integers(0, 100, rng.integers(1, 40)).astype(float)
        if vol.sum() == 0:
            continue
        poc = int(np.argmax(vol))
        a, z = vp.value_area(vol, poc, 0.7)
        assert a <= poc <= z
        assert vol[a : z + 1].sum() >= 0.7 * vol.sum() - 1e-9


def test_value_area_tie_goes_to_ref_side():
    vol = np.array([1.0, 5.0, 10.0, 5.0, 1.0])
    assert vp.value_area(vol, 2, 0.6, prefer_up=True) == (2, 4)
    assert vp.value_area(vol, 2, 0.6, prefer_up=False) == (0, 2)


def test_poc_tie_uses_center():
    fp = _one(10.00, 5) + _one(10.10, 5) + _one(10.12, 1)
    lv = vp.levels(fp, 1)
    assert lv is not None and lv.poc == pytest.approx(10.10)


def test_insignificant_valley_merges_peaks():
    s = np.array([0, 5, 10, 8, 9, 10.5, 4, 0], dtype=float)
    hv, lv = vp.nodes(s, 0.3, 0.3)
    assert hv == [5] and lv == []
