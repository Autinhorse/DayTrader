"""指标正确性（DESIGN.md 13）。

与 TA-Lib 对比；会话指标手算核对；盘中启动与从头计算一致；自定义指标自动发现。
"""

from __future__ import annotations

import random
import textwrap
from datetime import date, time

import numpy as np
import polars as pl
import pytest
import talib

from trader.core.models import Bar
from trader.core.timeutil import NS_PER_MIN, ny_to_ns
from trader.core.trading_calendar import TradingCalendar
from trader.data.catalog import Catalog
from trader.data.history import HistoryService
from trader.data.indicator_service import IndicatorService
from trader.data.store import BAR_SCHEMA, BarStore, fingerprint
from trader.data.validate import validate_day
from trader.indicators.base import (
    Indicator,
    IndicatorParams,
    OutputSpec,
    create,
    get_indicator,
    list_indicators,
    load_builtin,
    load_user,
    register_indicator,
)

load_builtin()
CAL = TradingCalendar()


def random_bars(n: int, seed: int = 7) -> list[Bar]:
    rnd = random.Random(seed)
    out, price = [], 100.0
    for i in range(n):
        o = price
        c = max(1.0, o * (1 + rnd.gauss(0, 0.004)))
        h = max(o, c) * (1 + abs(rnd.gauss(0, 0.002)))
        lo = min(o, c) * (1 - abs(rnd.gauss(0, 0.002)))
        out.append(Bar("X", "1m", i * NS_PER_MIN, o, h, lo, c, float(rnd.randint(100, 9000))))
        price = c
    return out


BARS = random_bars(3000)
H = np.array([b.high for b in BARS])
L = np.array([b.low for b in BARS])
C = np.array([b.close for b in BARS])
V = np.array([b.volume for b in BARS])


def run(name: str, key: str, **params) -> np.ndarray:
    ind = create(name, **params)
    vals = [ind.update(b)[key] for b in BARS]
    return np.array([np.nan if v is None else v for v in vals])


def same(ours: np.ndarray, ref: np.ndarray, skip: int = 0, rtol: float = 1e-9) -> None:
    np.testing.assert_allclose(ours[skip:], ref[skip:], rtol=rtol, equal_nan=True)


def test_registry_has_all_builtins():
    names = {c.name for c in list_indicators()}
    assert {
        "sma",
        "ema",
        "vwap",
        "bollinger",
        "rsi",
        "macd",
        "atr",
        "volume_ma",
        "opening_range",
        "prev_day",
    } <= names


@pytest.mark.parametrize("period", [5, 20, 50])
def test_sma_ema_vs_talib(period):
    same(run("sma", "value", period=period), talib.SMA(C, period))
    same(run("ema", "value", period=period), talib.EMA(C, period))


def test_bollinger_vs_talib():
    up, mid, lo = talib.BBANDS(C, 20, 2.0, 2.0)
    same(run("bollinger", "upper", period=20, k=2.0), up)
    same(run("bollinger", "middle", period=20, k=2.0), mid)
    same(run("bollinger", "lower", period=20, k=2.0), lo)


@pytest.mark.parametrize("period", [6, 14])
def test_rsi_atr_vs_talib(period):
    same(run("rsi", "value", period=period), talib.RSI(C, period))
    same(run("atr", "value", period=period), talib.ATR(H, L, C, period))


def test_macd_vs_talib():
    m, s, h = talib.MACD(C, 12, 26, 9)
    # TA-Lib 对快线的初值处理不同，预热足够长后一致
    skip = 26 * 10
    same(run("macd", "macd", fast=12, slow=26, signal=9), m, skip, rtol=1e-7)
    same(run("macd", "signal", fast=12, slow=26, signal=9), s, skip, rtol=1e-6)
    same(run("macd", "hist", fast=12, slow=26, signal=9), h, skip * 2, rtol=1e-4)


def test_volume_ma_vs_talib():
    same(run("volume_ma", "value", period=20), talib.SMA(V, 20))


def test_params_validation():
    with pytest.raises(ValueError):
        create("sma", period=0)
    with pytest.raises(ValueError):
        create("sma", period=10, nonsense=1)
    with pytest.raises(ValueError):
        create("macd", fast=30, slow=26)


# ---------- 会话指标 ----------

D1, D2 = date(2026, 1, 5), date(2026, 1, 6)


def day_bars(day: date, base: float) -> pl.DataFrame:
    """盘前 08:00–09:30 和常规时段每分钟一根 1 秒 bar，价格逐分钟 +0.01。"""
    rows = []
    t = ny_to_ns(day, time(8))
    end = CAL.trading_day(day).close
    i = 0
    while t < end:
        p = base + i * 0.01
        rows.append((t, p, p + 0.05, p - 0.05, p, 100.0 + i, p, 1))
        t += NS_PER_MIN
        i += 1
    return pl.DataFrame(rows, schema=BAR_SCHEMA, orient="row")


@pytest.fixture
def service(tmp_path):
    store = BarStore(tmp_path)
    catalog = Catalog(tmp_path / "catalog.sqlite")
    for d, base in ((D1, 100.0), (D2, 110.0)):
        df = day_bars(d, base)
        store.write_day("AAA", d, df)
        catalog.record("AAA", d, validate_day(df, CAL.trading_day(d)), fingerprint(df), 0)
    return IndicatorService(HistoryService(tmp_path, CAL, catalog))


def whole(day: date) -> tuple[int, int]:
    td = CAL.trading_day(day)
    return td.start, td.end


def test_vwap_anchor_regular(service):
    out = service.compute("AAA", "1m", "vwap", {}, *whole(D2))
    td = CAL.trading_day(D2)
    pre = out.filter(pl.col("ts_start") < td.open)
    assert pre["value"].null_count() == pre.height  # 盘前没有值
    first = out.filter(pl.col("ts_start") == td.open)["value"].item()
    assert first == pytest.approx(110.0 + 90 * 0.01)  # 09:30 是第 90 分钟
    day = out.filter(pl.col("ts_start") >= td.open)
    df = day_bars(D2, 110.0).filter(pl.col("ts_start") >= td.open)
    expect = float((df["vwap"] * df["volume"]).sum()) / float(df["volume"].sum())
    assert day["value"].to_list()[-1] == pytest.approx(expect)


def test_vwap_anchor_day_includes_premarket(service):
    out = service.compute("AAA", "1m", "vwap", {"anchor": "day"}, *whole(D2))
    assert out["value"].to_list()[0] == pytest.approx(110.0)


def test_opening_range(service):
    td = CAL.trading_day(D2)
    out = service.compute("AAA", "5m", "opening_range", {"minutes": 30}, *whole(D2))
    before = out.filter(pl.col("ts_end") < td.open + 30 * NS_PER_MIN)
    assert before["high"].null_count() == before.height
    after = out.filter(pl.col("ts_start") >= td.open + 30 * NS_PER_MIN)
    # 09:30–10:00 共 30 根分钟 bar：第 90～119 分钟
    assert after["high"].unique().to_list() == [pytest.approx(110 + 119 * 0.01 + 0.05)]
    assert after["low"].unique().to_list() == [pytest.approx(110 + 90 * 0.01 - 0.05)]


def test_prev_day(service):
    out = service.compute("AAA", "1m", "prev_day", {}, *whole(D2))
    d1 = day_bars(D1, 100.0).filter(pl.col("ts_start") >= CAL.trading_day(D1).open)
    row = out.row(0, named=True)
    assert row["high"] == pytest.approx(d1["high"].max())
    assert row["low"] == pytest.approx(d1["low"].min())
    assert row["close"] == pytest.approx(d1["close"].to_list()[-1])


@pytest.mark.parametrize(
    ("name", "params"),
    [("vwap", {}), ("vwap", {"anchor": "day"}), ("opening_range", {}), ("prev_day", {})],
)
def test_intraday_start_matches_full_day(service, name, params):
    """盘中启动（例如 11:17）与从交易日开始计算，在同一时刻的数值完全相同。"""
    td = CAL.trading_day(D2)
    full = service.compute("AAA", "1m", name, params, *whole(D2))
    mid = ny_to_ns(D2, time(11, 17))
    part = service.compute("AAA", "1m", name, params, mid, td.end)
    assert part.height > 0
    assert full.filter(pl.col("ts_start") >= mid).equals(part)


def test_continuous_warmup_crosses_days(service):
    """EMA 从 D2 开盘开始算，预热数据来自 D1：第一根就有值。"""
    td = CAL.trading_day(D2)
    out = service.compute("AAA", "1m", "ema", {"period": 20}, td.open, td.end)
    assert out["value"][0] is not None


# ---------- 自定义指标 ----------


def test_user_indicator_discovered(tmp_path, service):
    user = tmp_path / "user_indicators"
    user.mkdir()
    (user / "range_pct.py").write_text(
        textwrap.dedent(
            """
            from trader.indicators.base import (
                Indicator, IndicatorParams, OutputSpec, register_indicator,
            )

            class P(IndicatorParams):
                scale: float = 100.0

            @register_indicator
            class RangePct(Indicator):
                name = "test_range_pct"
                Params = P
                outputs = [OutputSpec("value", "histogram", "separate")]

                def update(self, bar):
                    return {"value": (bar.high - bar.low) / bar.close * self.params.scale}
            """
        ),
        encoding="utf-8",
    )
    (user / "broken.py").write_text("raise RuntimeError('故意出错')\n", encoding="utf-8")
    errors = load_user(user)
    assert list(errors) == ["broken.py"]  # 出错的文件不影响其他指标
    assert get_indicator("test_range_pct").name == "test_range_pct"
    out = service.compute("AAA", "1m", "test_range_pct", {}, *whole(D2))
    assert out["value"].null_count() == 0
    # 热加载：同一个文件再加载一次不会报重名
    assert load_user(user) == errors


def test_duplicate_name_rejected():
    class P(IndicatorParams):
        pass

    with pytest.raises(ValueError):

        @register_indicator
        class Fake(Indicator):
            name = "sma"
            Params = P
            outputs = [OutputSpec("value", "line", "price")]

            def update(self, bar):
                return {"value": 0.0}
