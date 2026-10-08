"""notebook 接口（DESIGN.md 10.6）：返回 pandas，时间为纽约时间；回测、读取、参数扫描。"""

from __future__ import annotations

import textwrap
from datetime import date

import pandas as pd
import pytest

from conftest import make_dataset

STRATEGY = textwrap.dedent(
    """
    from trader.core.models import Bar, Reason
    from trader.strategy.base import Strategy, StrategyParams, register_strategy


    class P(StrategyParams):
        qty: int = 10


    @register_strategy
    class NbBuy(Strategy):
        name = "nb_buy_twice"
        Params = P

        def on_start(self) -> None:
            self.ctx.subscribe("AAA", "5m")

        def on_session_open(self) -> None:
            self.ctx.state["n"] = 0  # 每天重新计数

        def on_bar(self, bar: Bar) -> None:
            if bar.timeframe != "5m":
                return
            flat = not self.ctx.position("AAA").qty and not self.ctx.open_orders("AAA")
            if flat and self.ctx.state.get("n", 0) < 2:
                self.ctx.buy("AAA", self.params.qty, reason=Reason("nb_buy"))
                self.ctx.state["n"] = self.ctx.state.get("n", 0) + 1
    """
)


@pytest.fixture
def project(tmp_path, monkeypatch):
    ds = make_dataset(tmp_path, [date(2026, 1, 5), date(2026, 1, 6)])
    (tmp_path / "user" / "strategies" / "nb_buy.py").write_text(STRATEGY, encoding="utf-8")
    monkeypatch.setenv("TRADER_HOME", str(tmp_path))
    monkeypatch.setenv("TRADER_DATA_DIR", str(tmp_path))
    return ds


def test_load_bars_and_indicator(project):
    from trader.research import compute_indicator, load_bars

    bars = load_bars("AAA", "5m", "2026-01-05", date(2026, 1, 6))
    assert isinstance(bars, pd.DataFrame) and len(bars) == 2 * 78
    idx = pd.DatetimeIndex(bars.index)
    assert str(idx.tz) == "America/New_York"
    assert f"{idx.min():%H:%M}" == "09:30"
    assert {"open", "high", "low", "close", "volume", "session"} <= set(bars.columns)
    ema = compute_indicator("AAA", "5m", "ema", "2026-01-06", "2026-01-06", period=10)
    assert len(ema) == 78
    assert bool(ema["value"].notna().all())  # 预热来自前一天


def test_run_backtest_and_load_run(project):
    from trader.research import load_run, run_backtest

    res = run_backtest("nb_buy_twice", {"qty": 20}, "2026-01-05", "2026-01-06", name="nb")
    # 每天开盘后买入一次，收盘前被强制平仓；收盘时段再买会被风控拒绝
    assert res.summary["counts"]["trades"] == 2
    assert isinstance(res.trades, pd.DataFrame) and len(res.trades) == 2
    assert str(pd.DatetimeIndex(res.trades.index).tz) == "America/New_York"
    assert (res.trades["qty"] == 20).all()
    again = load_run(res.run_id)
    assert again.summary == res.summary
    assert "净盈亏" in repr(again)


def test_sweep(project):
    from trader.research import sweep

    table = sweep(
        "nb_buy_twice",
        {},
        {"qty": [5, 10]},
        "2026-01-05",
        "2026-01-06",
        split="2026-01-06",
        workers=1,  # 多进程路径已在界面中验证；测试用单进程，快
    )
    assert isinstance(table, pd.DataFrame) and len(table) == 2
    assert set(table["param_qty"]) == {"5", "10"}
    assert {"in_net_pnl", "out_net_pnl", "in_run_id", "out_run_id"} <= set(table.columns)


def test_project_dir_found_from_subdirectory(tmp_path, monkeypatch):
    """notebook 放在项目子目录里：向上找到有 config/universe.yaml 的项目根目录。"""
    from trader.config import project_dir

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "universe.yaml").write_text("symbols: [SPY]\n", encoding="utf-8")
    sub = tmp_path / "user" / "notebooks"
    sub.mkdir(parents=True)
    monkeypatch.delenv("TRADER_HOME", raising=False)
    monkeypatch.chdir(sub)
    assert project_dir() == tmp_path


def test_strategy_defined_without_source_file_can_run():
    """在 notebook 单元格里定义的策略拿不到源文件：源码哈希记为不可用，回测照常运行。"""
    from trader.backtest.runner import _source_hash

    ns: dict = {}
    exec("class X:\n    pass\n", ns)  # noqa: S102
    assert _source_hash(ns["X"]) == "unavailable"
    from trader.strategy.base import StrategyState  # 定义在源文件里的类

    assert len(_source_hash(StrategyState)) == 16
