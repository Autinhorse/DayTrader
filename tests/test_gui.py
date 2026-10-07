"""研究版界面（阶段 4）：表单生成、参数扫描解析、实验记录与对比、图表数据，以及主窗口冒烟测试。

界面在无显示环境（offscreen）下运行；只验证逻辑和控件能否正常创建，外观靠截图人工检查。
"""

from __future__ import annotations

import os
from datetime import date
from typing import Literal

import pytest
from pydantic import Field

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")

from conftest import make_dataset  # noqa: E402
from trader.backtest import experiments as ex  # noqa: E402
from trader.backtest.config import BacktestConfig  # noqa: E402
from trader.backtest.runner import run_backtest  # noqa: E402
from trader.backtest.sweep import expand_grid, parse_values, plan  # noqa: E402
from trader.core.clock import SimClock  # noqa: E402
from trader.core.scheduler import EventScheduler  # noqa: E402
from trader.strategy.base import StrategyParams  # noqa: E402

pytest.importorskip("pytestqt")


class DemoParams(StrategyParams):
    symbol: str = "AAA"
    mode: Literal["a", "b"] = "a"
    period: int = Field(default=20, ge=1, le=100, description="周期")
    ratio: float = 1.5
    enabled: bool = True
    weights: dict[str, float] = Field(default_factory=lambda: {"x": 1.0})


# ---------- 参数扫描解析 ----------


def test_parse_values_and_grid():
    assert parse_values("5, 9 ,13") == ["5", "9", "13"]
    assert parse_values("5:20:5") == ["5", "10", "15", "20"]
    assert parse_values("0.5:1.5:0.5") == ["0.5", "1", "1.5"]
    assert parse_values("7") == ["7"]
    with pytest.raises(ValueError):
        parse_values("5:1:1")
    assert expand_grid({"b": [1, 2], "a": ["x"]}) == [{"a": "x", "b": 1}, {"a": "x", "b": 2}]


def test_plan_with_split():
    cfg = BacktestConfig(
        strategy="s", start=date(2026, 1, 5), end=date(2026, 1, 30), params={"p": 1}
    )
    jobs = plan(cfg, {"fast": ["5", "9"]}, date(2026, 1, 20))
    assert [(j[0]["fast"], j[1]) for j in jobs] == [
        ("5", "in"),
        ("5", "out"),
        ("9", "in"),
        ("9", "out"),
    ]
    ins = jobs[0][2]
    assert ins.end == date(2026, 1, 19) and ins.params == {"p": 1, "fast": "5"}
    assert jobs[1][2].start == date(2026, 1, 20)
    with pytest.raises(ValueError):
        plan(cfg, {}, date(2026, 3, 1))


# ---------- 表单 ----------


def test_model_form_roundtrip_and_sweep(qtbot):
    from trader.gui.forms import ModelForm

    f = ModelForm(DemoParams, {"period": 30}, sweep=True)
    qtbot.addWidget(f)
    assert f.values() == {
        "symbol": "AAA",
        "mode": "a",
        "period": 30,
        "ratio": 1.5,
        "enabled": True,
        "weights": {"x": 1.0},
    }
    f.widgets["period"].setText("5,10,15")  # type: ignore[attr-defined]
    f.widgets["ratio"].setText("1:2:0.5")  # type: ignore[attr-defined]
    assert f.sweep_grid() == {"period": ["5", "10", "15"], "ratio": ["1", "1.5", "2"]}
    assert f.values()["period"] == 5  # 扫描字段取第一个值做校验
    f.widgets["period"].setText("500")  # type: ignore[attr-defined]
    with pytest.raises(ValueError):
        f.values()  # 超出范围


# ---------- 实验记录、对比、图表数据 ----------


@pytest.fixture
def ds_with_runs(tmp_path):
    import trader.strategy.base as sb

    ds = make_dataset(tmp_path, [date(2026, 1, 5), date(2026, 1, 6)])
    # 测试里注册的 test_swing 策略来自 test_engine；这里注册一个最简单的
    if "gui_buy_once" not in {c.name for c in sb.list_strategies()}:
        from trader.core.models import Bar, Reason

        @sb.register_strategy
        class BuyOnce(sb.Strategy):
            name = "gui_buy_once"
            Params = StrategyParams

            def on_start(self) -> None:
                self.ctx.subscribe("AAA", "5m")

            def on_bar(self, bar: Bar) -> None:
                flat = not self.ctx.position("AAA").qty and not self.ctx.open_orders("AAA")
                if flat and self.ctx.state.get("n", 0) < 2:
                    self.ctx.buy("AAA", 10, reason=Reason("buy"))
                    self.ctx.state["n"] = self.ctx.state.get("n", 0) + 1

    runs = []
    for i, end in enumerate((date(2026, 1, 5), date(2026, 1, 6))):
        cfg = BacktestConfig(name=f"r{i}", strategy="gui_buy_once", start=date(2026, 1, 5), end=end)
        r = run_backtest(
            cfg,
            data_dir=ds.root,
            project_dir=ds.root,
            clock=SimClock(EventScheduler(), i * 10**9),
            index_extra={"tags": "t1"},
        )
        runs.append(r)
    return ds, runs


def test_experiments_index_and_compare(ds_with_runs):
    ds, runs = ds_with_runs
    index = ds.root / "runs" / "index.sqlite"
    rows = ex.list_runs(index)
    assert {r["name"] for r in rows} == {"r0", "r1"}
    ex.update(index, rows[0]["run_id"], name="改名", notes="备注", tags="a,b")
    assert ex.list_runs(index, text="备注")[0]["name"] == "改名"
    assert ex.list_runs(index, strategy="nope") == []
    loaded = [ex.load_run(r.out_dir) for r in runs if r.out_dir]
    diffs = dict(ex.config_differences(loaded))
    assert "end" in diffs and "name" in diffs and "strategy" not in diffs
    assert ex.format_metric(0.5, "pct") == "50.0%"


def test_chart_payload(ds_with_runs):
    from trader.gui.services import IndicatorSpec, Services

    ds, runs = ds_with_runs
    sv = Services(ds.root, ds.root)
    days = sv.window_days("AAA", "5m", date(2026, 1, 6))
    assert [d.day for d in days] == [date(2026, 1, 5), date(2026, 1, 6)]
    p = sv.chart_payload(
        "AAA",
        "5m",
        "rth",
        days,
        [IndicatorSpec("ema", {"period": 5}), IndicatorSpec("bollinger", {})],
    )
    assert len(p["candles"]) == 2 * 78
    times = [c["time"] for c in p["candles"]]
    assert times == sorted(times)
    # 时间是纽约当地时间：第一根是 09:30
    assert times[0] % 86400 == 9 * 3600 + 30 * 60
    ema = p["indicators"][0]["outputs"][0]
    assert ema["key"] == "value" and len(ema["data"]) == 2 * 78 - 4  # 前 4 根还在预热
    assert [o["key"] for o in p["indicators"][1]["outputs"]] == ["upper", "middle", "lower"]
    markers = sv.trade_markers(runs[1].fills, "AAA")
    assert markers and markers[0]["shape"] == "arrowUp"


# ---------- 主窗口冒烟测试 ----------


def test_main_window_smoke(qtbot, ds_with_runs, tmp_path):
    from trader.gui.main_window import MainWindow

    ds, runs = ds_with_runs
    w = MainWindow(ds.root, ds.root, settings_path=tmp_path / "gui.ini")
    qtbot.addWidget(w)
    assert len(w.charts) == 1
    w.new_chart({"symbol": "AAA", "timeframe": "5m"})
    assert len(w.charts) == 2
    w.open_run(str(runs[1].out_dir))
    assert w.results.run is not None and w.results.trades.visible_count() >= 1
    # 点击第一笔交易：联动的图表切到该标的并加载数据
    w.results._trade_clicked(w.results.trades.model().index(0, 0))
    _, chart = list(w.charts.values())[0]
    assert chart.symbol == "AAA" and chart.overlay_fills is not None and chart.days
    w.experiments.refresh()
    assert w.experiments.table.visible_count() == 2
    w.save_layout()
    w2 = MainWindow(ds.root, ds.root, settings_path=tmp_path / "gui.ini")
    qtbot.addWidget(w2)
    assert len(w2.charts) == 2  # 打开的图表被恢复
    assert sorted(c.timeframe for _, c in w2.charts.values()) == ["1m", "5m"]
