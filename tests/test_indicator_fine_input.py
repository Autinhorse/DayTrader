"""细粒度输入的指标（决策 0018）：用 1 秒 bar 计算、按显示周期取值；
图表、回放、策略结果一致，无未来数据。以及 Volume Profile 指标与研究模块的结果一致。"""

from __future__ import annotations

from datetime import date, time

import pytest

from conftest import CAL, make_dataset
from test_engine import engine
from trader.analytics.volume_profile import ProfileParams, VolumeProfile
from trader.backtest.replay import ReplaySession, ReplaySettings
from trader.brokers.sim.matcher import SimConfig
from trader.core.models import Bar
from trader.core.timeutil import NS_PER_SEC, ny_to_ns
from trader.core.trading_calendar import TradingDay
from trader.data.indicator_service import IndicatorService
from trader.indicators.base import (
    Indicator,
    IndicatorParams,
    OutputSpec,
    WarmupSpec,
    register_indicator,
)
from trader.strategy.base import Strategy, StrategyParams, register_strategy

DAYS = [date(2026, 1, d) for d in (5, 6, 7, 8, 9, 12, 13, 14)]


@register_indicator
class FineVolume(Indicator):
    """当天已喂入的 1 秒 bar 成交量之和，以及最后一根的收盘时刻。"""

    name = "test_fine_volume"
    Params = IndicatorParams
    outputs = [OutputSpec("vol", "line", "separate"), OutputSpec("last", "line", "separate")]
    scope = "session"
    input_timeframe = "1s"

    def __init__(self, params: IndicatorParams) -> None:
        super().__init__(params)
        self.vol, self.last = 0.0, 0

    def warmup(self) -> WarmupSpec:
        return WarmupSpec(from_session_start=True)

    def on_session_open(self, day: TradingDay) -> None:
        self.vol, self.last = 0.0, 0

    def ingest(self, bar: Bar) -> None:
        assert bar.timeframe == "1s"
        self.vol += bar.volume
        self.last = bar.ts_end

    def update(self, bar: Bar) -> dict[str, float | None]:
        return {"vol": self.vol, "last": float(self.last)}


@pytest.fixture(scope="module")
def ds(tmp_path_factory):
    return make_dataset(tmp_path_factory.mktemp("fine"), DAYS)


def test_service_samples_at_display_close(ds):
    td = CAL.trading_day(DAYS[1])
    df = IndicatorService(ds.history).compute(
        "AAA", "5m", "test_fine_volume", {}, td.open, td.close, "rth"
    )
    sec = ds.history.bars("AAA", "1s", td.open, td.close, "rth")
    ends, vols = sec["ts_end"].to_numpy(), sec["volume"].to_numpy()
    assert df.height == 78
    for ts_end, vol, last in df.select("ts_end", "vol", "last").iter_rows():
        m = ends <= ts_end
        assert vol == pytest.approx(vols[m].sum())  # 收盘之前的 1 秒 bar 全部喂入
        assert last == ends[m].max()  # 之后的一根也没有


def test_display_equals_input_timeframe(ds):
    td = CAL.trading_day(DAYS[1])
    df = IndicatorService(ds.history).compute(
        "AAA", "1s", "test_fine_volume", {}, td.open, td.open + 60 * NS_PER_SEC, "rth"
    )
    assert (df["last"] == df["ts_end"]).all()  # 当前这根已经喂入


def test_replay_watch_matches_chart(ds):
    """回放从一根 5 分钟 bar 的中间开始：活动实例预热时也喂入这根 bar 已收盘的部分，
    之后逐秒喂入。"""
    day = DAYS[2]
    rs = ReplaySession(
        ds.history,
        ReplaySettings(day=day, symbols=["AAA"], start_time=time(10, 2, 30),
                       sim=SimConfig(half_spread_bps={"default": 0.0}, impact_k=0.0)),
    )  # fmt: skip
    h = rs.watch_indicator("AAA", "5m", "test_fine_volume", {})
    rs.advance_to(ny_to_ns(day, time(12, 0)))
    td = CAL.trading_day(day)
    want = IndicatorService(ds.history).compute(
        "AAA", "5m", "test_fine_volume", {}, td.open, td.close, "rth"
    )
    want_by_ts = {r[0]: (r[1], r[2]) for r in want.select("ts_start", "vol", "last").iter_rows()}
    got = [(t, v) for t, v in zip(h.times, h.values, strict=True) if t > 0]
    assert len(got) >= 20
    for t, v in got:
        assert (v["vol"], v["last"]) == pytest.approx(want_by_ts[t])


class FineParams(StrategyParams):
    pass


@register_strategy
class UsesFine(Strategy):
    name = "test_uses_fine"
    Params = FineParams

    def on_start(self) -> None:
        self.ind = self.ctx.indicator("test_fine_volume", "AAA", "5m")
        self.seen: set[str] = set()
        self.samples: list[tuple[int, float | None]] = []

    def on_bar(self, bar: Bar) -> None:
        self.seen.add(bar.timeframe)
        self.samples.append((bar.ts_end, self.ind.get("last")))


def test_strategy_gets_only_its_timeframe(ds):
    e = engine(ds)
    td = CAL.trading_day(DAYS[3])
    e.run(UsesFine, FineParams(), td.start, td.end)
    s = e.strategy
    assert isinstance(s, UsesFine)
    assert s.seen == {"5m"}  # 指标订阅的 1 秒 bar 不回调策略
    assert all(last is not None and last <= ts_end for ts_end, last in s.samples)


# ---------- Volume Profile 指标与研究模块一致（研究模块关掉清洗） ----------


def _research(ds) -> VolumeProfile:
    return VolumeProfile(ProfileParams(print_filter_adr=1e9, bad_windows=()), ds.history)


@pytest.mark.parametrize("days", [1, 3])
def test_vp_levels_match_research(ds, days):
    day = DAYS[-1]
    td = CAL.trading_day(day)
    df = IndicatorService(ds.history).compute(
        "AAA", "5m", "vp_levels", {"days": days}, td.open, td.close, "rth"
    )
    kind, kw = ("daily", {}) if days == 1 else ("composite", {"n": days})
    want = _research(ds).get_levels("AAA", day, kind, **kw)
    for k in ("poc", "vah", "val"):
        assert df[k].n_unique() == 1  # 一整天不变
        assert df[k][0] == pytest.approx(want[k]), k


def test_vp_developing_matches_research(ds):
    day = DAYS[-2]
    td = CAL.trading_day(day)
    df = IndicatorService(ds.history).compute(
        "AAA", "1m", "vp_developing", {}, td.open, td.close, "rth"
    )
    want = _research(ds).get_developing("AAA", day)
    assert df.height == len(want) == 390
    for a, b in (("dpoc", "dpoc"), ("dvah", "dvah"), ("dval", "dval")):
        assert df[a].to_list() == pytest.approx(want[b].to_list()), a


@pytest.mark.parametrize("name,params", [("vp_developing", {}), ("vp_levels", {"days": 3})])
def test_vp_engine_per_bar_matches_batch(ds, name, params):
    """引擎逐根喂入（回放、回测、实盘）与图表批量喂入的结果相同。"""
    day = DAYS[-2]
    rs = ReplaySession(
        ds.history,
        ReplaySettings(day=day, symbols=["AAA"], start_time=time(10, 2, 30),
                       sim=SimConfig(half_spread_bps={"default": 0.0}, impact_k=0.0)),
    )  # fmt: skip
    h = rs.watch_indicator("AAA", "5m", name, params)
    rs.advance_to(CAL.trading_day(day).close + NS_PER_SEC)
    td = CAL.trading_day(day)
    want = IndicatorService(ds.history).compute("AAA", "5m", name, params, td.open, td.close, "rth")
    keys = [k for k in want.columns if k not in ("ts_start", "ts_end")]
    by_ts = {r[0]: r[1:] for r in want.select("ts_start", *keys).iter_rows()}
    got = [(t, v) for t, v in zip(h.times, h.values, strict=True) if t > 0]
    assert len(got) > 60
    for t, v in got:
        assert tuple(v[k] for k in keys) == pytest.approx(by_ts[t])
