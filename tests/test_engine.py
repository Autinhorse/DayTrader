"""引擎与回测（DESIGN.md 13）：事件阶段顺序、无未来数据、确定性、策略状态恢复、
引擎 bar 与历史查询一致、行情稀疏时定时器照常触发、性能。"""

from __future__ import annotations

import random
import time as _time
from datetime import date, time
from decimal import Decimal
from typing import Any, cast

import polars as pl
import pytest

from conftest import CAL, Dataset, make_dataset
from trader.backtest.config import BacktestConfig
from trader.backtest.runner import business_view, run_backtest
from trader.brokers.sim.matcher import SimBroker, SimConfig
from trader.core.clock import SimClock
from trader.core.models import Bar, Fill, Order, Reason
from trader.core.scheduler import EventScheduler
from trader.core.timeutil import NS_PER_MIN, ny_to_ns
from trader.data.store import BAR_SCHEMA, BarStore, fingerprint
from trader.data.validate import validate_day
from trader.engine.engine import BacktestEngine, EngineConfig
from trader.indicators.base import load_builtin
from trader.oms.risk import RiskLimits
from trader.strategy.base import Strategy, StrategyParams, register_strategy

load_builtin()
D1 = date(2026, 1, 5)


def engine(ds: Dataset, **cfg: Any) -> BacktestEngine:
    sim = SimConfig(half_spread_bps={"default": 0.0}, impact_k=0.0)
    base: dict[str, Any] = {"strategy_id": "t", "whitelist": frozenset({"AAA", "BBB"})}
    base.update(cfg)
    return BacktestEngine(ds.history, SimBroker(sim), RiskLimits(), EngineConfig(**base))


def run_days(e: BacktestEngine, cls, params, days):
    return e.run(cls, params, days[0].start, days[-1].end)


# ---------- 阶段顺序与生效时间 ----------


class ProbeParams(StrategyParams):
    order_at: str = "10:01:57"  # 在这根 1 秒 bar 收盘后下市价单


@register_strategy
class Probe(Strategy):
    name = "test_probe"
    Params = ProbeParams

    def on_start(self) -> None:
        self.events: list[tuple[str, int, Any]] = []
        self.ctx.subscribe("AAA", "1s")
        self.ctx.subscribe("AAA", "1m")
        assert isinstance(self.params, ProbeParams)
        h, m, s = map(int, self.params.order_at.split(":"))
        self.trigger = (h, m, s)
        self.done = False

    def on_bar(self, bar: Bar) -> None:
        self.events.append((f"bar_{bar.timeframe}", self.ctx.now(), bar.ts_start))
        day = self.ctx.today
        if (
            bar.timeframe == "1s"
            and not self.done
            and day is not None
            and bar.ts_start == ny_to_ns(day.day, time(*self.trigger))
        ):
            self.ctx.buy("AAA", 10, reason=Reason("probe"))
            self.done = True

    def on_fill(self, fill: Fill) -> None:
        self.events.append(("fill", self.ctx.now(), fill.ts))


def test_order_fills_only_on_later_interval_and_phase_order(dataset):
    e = engine(dataset)
    r = run_days(e, Probe, ProbeParams(), dataset.days[:1])
    s = cast(Probe, e.strategy)
    fill = next(f for f in r.fills if f.client_order_id.startswith("t-"))
    # 10:01:57 这根 bar 在 10:01:58.3 才可见；订单不能用它，也不能用已经开始的 10:01:58 这根
    assert fill.ts == ny_to_ns(D1, time(10, 1, 59))
    bars = dataset.history.store.read_day("AAA", D1)
    open_ = bars.filter(pl.col("ts_start") == fill.ts)["open"].item()
    assert float(fill.price) == pytest.approx(open_)
    # 10:02:00.3：撮合 → 交付成交 → 发布收盘 bar（1 秒和 1 分钟）
    t = ny_to_ns(D1, time(10, 2, 0)) + 300_000_000
    at_t = [k for k, now, _ in s.events if now == t]
    assert at_t[0] == "fill" and "bar_1m" in at_t and at_t.index("fill") < at_t.index("bar_1m")


# ---------- 稀疏行情下定时器照常触发 ----------


@register_strategy
class TimerStrat(Strategy):
    name = "test_timer"
    Params = StrategyParams

    def on_start(self) -> None:
        self.fired: list[int] = []
        self.ctx.subscribe("AAA", "1m")
        self.ctx.set_timer("noon", ny_to_ns(D1, time(11, 30)))

    def on_bar(self, bar: Bar) -> None:
        pass

    def on_timer(self, name: str) -> None:
        self.fired.append(self.ctx.now())


def test_timer_fires_in_sparse_data(tmp_path):
    # 11:00–12:00 没有任何成交
    ds = make_dataset(tmp_path, [D1], keep=lambda d, i: not (5400 <= i < 9000))
    e = engine(ds)
    run_days(e, TimerStrat, StrategyParams(), ds.days)
    assert cast(TimerStrat, e.strategy).fired == [ny_to_ns(D1, time(11, 30))]


# ---------- 一个会交易的策略：用于无未来数据、确定性、状态恢复 ----------


class SwingParams(StrategyParams):
    period: int = 20


@register_strategy
class Swing(Strategy):
    """每天最多两次：收盘价上穿 SMA 买入，下穿卖出；当天交易次数存在 ctx.state 里。"""

    name = "test_swing"
    Params = SwingParams

    def on_start(self) -> None:
        assert isinstance(self.params, SwingParams)
        self.sma = self.ctx.indicator("sma", "AAA", "1m", period=self.params.period)

    def on_session_open(self) -> None:
        day = self.ctx.today
        if day is not None and self.ctx.state.get("day") != day.day.isoformat():
            self.ctx.state["day"] = day.day.isoformat()
            self.ctx.state["entries"] = 0

    def on_bar(self, bar: Bar) -> None:
        v = self.sma.value
        if v is None or self.ctx.open_orders("AAA"):
            return
        pos = self.ctx.position("AAA").qty
        if pos == 0 and bar.close > v and self.ctx.state.get("entries", 0) < 2:
            self.ctx.buy("AAA", 50, reason=Reason("cross_up", context={"sma": v}))
            self.ctx.state["entries"] = self.ctx.state.get("entries", 0) + 1
        elif pos > 0 and bar.close < v:
            self.ctx.close_position("AAA", reason=Reason("cross_down"))


def order_rows(orders: list[Order], before: int | None = None) -> list[tuple]:
    return [
        (
            o.created_at,
            o.intent.side,
            o.intent.qty,
            o.intent.reason.code,
            o.status.value,
            o.avg_fill_price,
        )
        for o in sorted(orders, key=lambda o: o.created_at)
        if before is None or o.created_at < before
    ]


def test_no_lookahead(tmp_path):
    """把 t 之后的数据换成随机值重跑，t 之前的订单必须完全相同。"""
    ds = make_dataset(tmp_path / "a", [D1, date(2026, 1, 6)])
    base = run_days(engine(ds), Swing, SwingParams(), ds.days)
    t = ny_to_ns(date(2026, 1, 6), time(11, 0))
    # 改写第二天 11:00 之后的数据
    store = BarStore(ds.root)
    df = store.read_day("AAA", date(2026, 1, 6))
    rnd = random.Random(99)
    late = (
        df.filter(pl.col("ts_start") >= t)
        .with_columns(
            [
                pl.Series(
                    c,
                    [
                        rnd.uniform(50, 150)
                        for _ in range(df.filter(pl.col("ts_start") >= t).height)
                    ],
                )
                for c in ("open", "close")
            ]
        )
        .with_columns(
            pl.max_horizontal("open", "close").alias("high"),
            pl.min_horizontal("open", "close").alias("low"),
        )
    )
    new = pl.concat([df.filter(pl.col("ts_start") < t), late.select(df.columns)])
    store.write_day("AAA", date(2026, 1, 6), new)
    td = CAL.trading_day(date(2026, 1, 6))
    ds.catalog.record("AAA", td.day, validate_day(new, td), fingerprint(new), 0)
    changed = run_days(engine(ds), Swing, SwingParams(), ds.days)
    # 只比较 t 之前作出的决策（下单时间、方向、数量、原因）；成交价可能落在 t 之后，合理地不同
    a = [r[:4] for r in order_rows(base.orders, t)]
    b = [r[:4] for r in order_rows(changed.orders, t)]
    assert a
    assert a == b
    # 数据在 t 之后确实被改过
    h = ds.history.bars("AAA", "1m", t, td.end)
    assert float(h["close"].max()) > 120 or float(h["close"].min()) < 80  # type: ignore[arg-type]


def test_determinism_via_runner(dataset):
    cfg = BacktestConfig(
        strategy="test_swing",
        start=date(2026, 1, 5),
        end=date(2026, 1, 7),
        sim=SimConfig(half_spread_bps={"default": 2.0}),
    )
    kw: dict[str, Any] = {
        "data_dir": dataset.root,
        "project_dir": dataset.root,
        "clock": SimClock(EventScheduler(), 0),
        "write": False,
    }
    a, b = run_backtest(cfg, **kw), run_backtest(cfg, **kw)
    assert a.trades.height > 0
    assert business_view(a) == business_view(b)


def test_runner_writes_all_files(dataset):
    cfg = BacktestConfig(strategy="test_swing", start=date(2026, 1, 5), end=date(2026, 1, 6))
    r = run_backtest(
        cfg,
        data_dir=dataset.root,
        project_dir=dataset.root,
        clock=SimClock(EventScheduler(), 0),
    )
    assert r.out_dir is not None
    names = {p.name for p in r.out_dir.iterdir()}
    assert names >= {
        "config.json",
        "orders.parquet",
        "fills.parquet",
        "trades.parquet",
        "equity.parquet",
        "marks.parquet",
        "log.jsonl",
        "summary.json",
    }
    assert (dataset.root / "runs" / "index.sqlite").exists()
    t = r.trades
    assert t.height and t["closed"].all()
    assert {"entry_reason", "exit_reason", "mfe", "mae", "net_pnl"} <= set(t.columns)
    s = r.summary
    assert s["cost_sensitivity"]["scenarios"][1]["net_pnl"] < s["pnl"]["net_pnl"]
    # 每天收盘前平仓：没有隔夜仓
    assert all(p.qty == 0 for p in r.engine.portfolio.positions.values())


def test_state_recovery_matches_uninterrupted(dataset):
    """跑完第一天停止，带着 ctx.state 从第二天继续，与不中断运行的结果相同，不重复下单。"""
    full = run_days(engine(dataset), Swing, SwingParams(), dataset.days)
    first = run_days(engine(dataset), Swing, SwingParams(), dataset.days[:1])
    resumed = run_days(
        engine(dataset, initial_state=first.state), Swing, SwingParams(), dataset.days[1:]
    )
    cut = dataset.days[1].start

    def rows(orders):
        return [r[1:4] + (r[0],) for r in order_rows(orders)]

    assert rows([o for o in full.orders if o.created_at >= cut]) == rows(resumed.orders)
    assert first.state["entries"] <= 2


# ---------- 引擎 bar 与历史查询一致（含官方 1 分钟成交量） ----------


@register_strategy
class Collect(Strategy):
    name = "test_collect"
    Params = StrategyParams

    def on_start(self) -> None:
        self.bars: list[Bar] = []
        self.ctx.subscribe("AAA", "5m")

    def on_bar(self, bar: Bar) -> None:
        self.bars.append(bar)


def test_engine_bars_equal_history(dataset):
    td = dataset.days[0]
    # 放一份"官方 1 分钟 bar"，成交量与 1 秒加总不同
    sec = dataset.history.store.read_day("AAA", td.day)
    minute = (
        sec.group_by(
            (pl.col("ts_start") // NS_PER_MIN * NS_PER_MIN).alias("m"), maintain_order=True
        )
        .agg(
            pl.col("open").first(),
            pl.col("high").max(),
            pl.col("low").min(),
            pl.col("close").last(),
        )
        .rename({"m": "ts_start"})
        .with_columns(
            pl.lit(12345.0).alias("volume"), pl.lit(100.0).alias("vwap"), pl.lit(7).alias("trades")
        )
        .select(list(BAR_SCHEMA))
        .cast(BAR_SCHEMA)  # type: ignore[arg-type]
    )
    BarStore(dataset.root, "1m").write_day("AAA", td.day, minute)
    dataset.catalog.record(
        "AAA", td.day, validate_day(minute, td), fingerprint(minute), 0, kind="bar_1m"
    )
    e = engine(dataset)
    run_days(e, Collect, StrategyParams(), [td])
    collected = cast(Collect, e.strategy).bars
    got = [(b.ts_start, b.open, b.high, b.low, b.close, b.volume, b.trades) for b in collected]
    hist = dataset.history.bars("AAA", "5m", td.start, td.end)
    want = [
        tuple(r)
        for r in hist.select(
            "ts_start", "open", "high", "low", "close", "volume", "trades"
        ).iter_rows()
    ]
    assert got == want
    assert got[0][5] == 12345.0 * 5


# ---------- 性能 ----------


def test_performance_one_symbol_one_day(dataset):
    """单标的单日 1 秒 bar 的回测在 2 秒内完成（DESIGN.md 10.2）。"""
    t0 = _time.perf_counter()
    r = run_days(engine(dataset), Swing, SwingParams(), dataset.days[:1])
    assert _time.perf_counter() - t0 < 2.0
    assert r.fills


def test_initial_cash_and_decimal(dataset):
    e = engine(dataset, initial_cash=Decimal(50_000))
    r = run_days(e, Swing, SwingParams(), dataset.days[:1])
    assert r.equity[0][1] == pytest.approx(50_000, rel=0.01)
