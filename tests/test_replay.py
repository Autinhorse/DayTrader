"""回放（DESIGN.md 13）。

回放与回测一致；回放查询不泄露未来数据；向后跳转开始新分支；单步、手动下单。
"""

from __future__ import annotations

from datetime import date, time

import polars as pl
import pytest

from conftest import CAL, make_dataset
from test_engine import Swing, SwingParams, engine, order_rows
from trader.backtest.replay import ReplaySession, ReplaySettings
from trader.brokers.sim.matcher import SimConfig
from trader.core.timeutil import NS_PER_MIN, NS_PER_SEC, ny_to_ns

D = date(2026, 1, 6)


@pytest.fixture
def ds(tmp_path):
    return make_dataset(tmp_path, [date(2026, 1, 5), D])


def session(ds, start: time = time(9, 30)) -> ReplaySession:
    return ReplaySession(
        ds.history,
        ReplaySettings(
            day=D,
            symbols=["AAA"],
            start_time=start,
            sim=SimConfig(half_spread_bps={"default": 0.0}, impact_k=0.0),
        ),
    )


def test_replay_matches_backtest(ds):
    """回放全速（分很多小步）跑完一天，订单与同一天的回测完全相同。"""
    td = CAL.trading_day(D)
    bt = engine(ds).run(Swing, SwingParams(), td.start, td.end)
    rs = session(ds)
    rs.attach_strategy(Swing, SwingParams())
    while not rs.finished:
        rs.advance_by(37 * NS_PER_SEC + 123)  # 不规则的步长
    got = [r for r in order_rows(rs.engine.result().orders)]
    want = [r for r in order_rows(bt.orders)]
    assert want and [r[1:] for r in got] == [r[1:] for r in want]
    assert [r[0] for r in got] == [r[0] for r in want]


def test_queries_never_see_future(ds):
    rs = session(ds)
    rs.partial("AAA", "5m")  # 先订阅（界面打开图表时就订阅），之后才有形成中的 bar
    rs.advance_to(ny_to_ns(D, time(11, 0, 37)) + 400_000_000)
    now, grace = rs.now, rs.engine.grace
    for tf in ("1s", "1m", "5m", "1h"):
        bars = rs.bars("AAA", tf)
        assert bars.height and int(bars["ts_end"].max()) + grace <= now  # type: ignore[arg-type]
    vals = rs.indicator_values("AAA", "1m", "ema", {"period": 9})
    assert int(vals["ts_end"].max()) + grace <= now  # type: ignore[arg-type]
    p = rs.partial("AAA", "5m")
    assert p is not None and p.ts_start <= now
    # 形成中的 bar 只包含已经可用的 1 秒 bar：收盘价等于最后一根可用 1 秒 bar 的收盘价
    last_1s = rs.bars("AAA", "1s").tail(1)
    assert p.close == last_1s["close"].item()
    # 数据本身在 now 之后还有（确实存在“未来”，只是没有泄露）
    td = CAL.trading_day(D)
    full = ds.history.bars("AAA", "1m", td.open, td.close)
    assert int(full["ts_end"].max()) > now  # type: ignore[arg-type]


def test_seek_back_starts_new_branch(ds):
    rs = session(ds)
    rs.advance_to(ny_to_ns(D, time(10, 0)))
    rs.order("AAA", "BUY", 10)
    rs.advance_by(5 * NS_PER_SEC)
    assert rs.engine.portfolio.position("AAA").qty == 10
    first = rs.branch
    rs.seek(ny_to_ns(D, time(9, 45)))
    assert len(rs.branches) == 2 and rs.branch is not first
    assert rs.now == ny_to_ns(D, time(9, 45))
    assert rs.engine.portfolio.position("AAA").qty == 0  # 新分支空仓
    assert first.engine.portfolio.position("AAA").qty == 10  # 原分支记录保留
    assert int(rs.bars("AAA", "1m")["ts_end"].max()) <= rs.now  # type: ignore[arg-type]
    # 向前跳：同一个分支，中间的事件照常处理
    rs.seek(ny_to_ns(D, time(10, 30)))
    assert len(rs.branches) == 2 and rs.now == ny_to_ns(D, time(10, 30))


def test_seek_back_restarts_strategy(ds):
    rs = session(ds)
    rs.attach_strategy(Swing, SwingParams())
    rs.advance_to(ny_to_ns(D, time(12, 0)))
    rs.seek(ny_to_ns(D, time(10, 0)))
    assert rs.engine.strategy is not None and isinstance(rs.engine.strategy, Swing)
    assert rs.engine.ctx.state.get("entries", 0) == 0


def test_step_bar(ds):
    rs = session(ds)
    bar = rs.step_bar("AAA", "1m")
    assert bar is not None and bar.ts_start == ny_to_ns(D, time(9, 30))
    assert rs.now == bar.ts_end + rs.engine.grace
    bar2 = rs.step_bar("AAA", "5m")
    assert bar2 is not None and bar2.ts_end == ny_to_ns(D, time(9, 35))


def test_manual_orders_and_summary(ds):
    rs = session(ds)
    rs.advance_to(ny_to_ns(D, time(10, 0)))
    o = rs.order("AAA", "BUY", 20)
    assert o.reject_rule is None
    rs.advance_by(3 * NS_PER_SEC)
    assert rs.engine.portfolio.position("AAA").qty == 20
    rs.advance_by(10 * NS_PER_MIN)
    rs.close_position("AAA")
    rs.advance_by(3 * NS_PER_SEC)
    assert rs.engine.portfolio.position("AAA").qty == 0
    s = rs.branch_summary()
    assert s["trades"] == 1 and s["open_positions"] == {}
    f = rs.fills("AAA")
    assert f.height == 2 and set(f["reason_code"]) == {"manual"}
    assert isinstance(s["trades_table"], pl.DataFrame)


def test_eod_flatten_applies_to_manual_positions(ds):
    rs = session(ds)
    rs.advance_to(ny_to_ns(D, time(15, 0)))
    rs.order("AAA", "BUY", 10)
    rs.advance_to(CAL.trading_day(D).close)
    assert rs.engine.portfolio.position("AAA").qty == 0
    assert any(o.intent.reason.code == "eod_flatten" for o in rs.engine.result().orders)


def test_manual_order_right_after_mid_day_start(ds):
    """从 10:00 开始回放立即下单：最新价来自之前最后一根 1 秒 bar，不会因“行情未更新”被拒。"""
    rs = session(ds, start=time(10, 0))
    o = rs.order("AAA", "BUY", 10)
    assert o.reject_rule is None
    last = rs.bars("AAA", "1s").tail(1)
    assert rs.engine.portfolio.last_price["AAA"] == last["close"].item()
