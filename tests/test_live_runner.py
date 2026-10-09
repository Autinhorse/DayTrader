"""local_paper 端到端（假的 IB 客户端）：启动检查、补数、实时 bar 驱动策略、模拟成交、
落盘与重启恢复、行情中断暂停策略、人工风控按钮、配置和入口的安全拒绝。"""

from __future__ import annotations

import asyncio
from datetime import date, time
from pathlib import Path

import pytest
import yaml
from tests.fake_ib import FakeIb

from conftest import make_dataset
from trader.brokers.ibkr.api import HistBar, SafetyError
from trader.config import SymbolConfig, Universe
from trader.core.models import Bar, OrderStatus, Reason
from trader.core.timeutil import NS_PER_MIN, NS_PER_SEC, ny_to_ns
from trader.core.trading_calendar import TradingCalendar
from trader.data.bar_builder import TradeTick
from trader.live.config import ConfigError, LiveConfig, load_live_config
from trader.live.runner import LiveRunner, StartupError
from trader.oms.risk import RiskLimits
from trader.strategy.base import Strategy, StrategyParams, register_strategy

CAL = TradingCalendar()
PREV, DAY = date(2026, 1, 5), date(2026, 1, 6)
TD = CAL.trading_day(DAY)
S = NS_PER_SEC
T0 = ny_to_ns(DAY, time(10, 30)) + 400 * 10**6


class BuyOnceParams(StrategyParams):
    symbol: str = "AAA"


@register_strategy
class BuyOnce(Strategy):
    """测试用：订阅 1 分钟 bar，第一根收盘 bar 买 10 股，之后不再交易。"""

    name = "test_live_buy_once"
    Params = BuyOnceParams

    def on_start(self) -> None:
        assert isinstance(self.params, BuyOnceParams)
        self.ctx.subscribe(self.params.symbol, "1m")
        self.bars: list[Bar] = []

    def on_bar(self, bar: Bar) -> None:
        self.bars.append(bar)
        if not self.ctx.state.get("entered"):
            self.ctx.buy(bar.symbol, 10, reason=Reason("test_entry"))
            self.ctx.state["entered"] = True


class Clock:
    def __init__(self, t: int) -> None:
        self.t = t

    def __call__(self) -> int:
        return self.t


def config(**kw) -> LiveConfig:
    base = {
        "profile": "local_paper",
        "symbols": ["AAA", "BBB"],
        "strategy": {"name": "test_live_buy_once", "params": {"symbol": "AAA"}},
        "risk": RiskLimits().model_dump(),
    }
    return LiveConfig.model_validate(base | kw)


UNIVERSE = Universe(symbols=[SymbolConfig(symbol="AAA", feed="tick"), SymbolConfig(symbol="BBB")])


def fake_ib(clock: Clock, **kw) -> FakeIb:
    ib = FakeIb(**kw)
    ib.clock = clock
    for sym in ("AAA", "BBB"):
        ib.hist[(sym, "1 min")] = [
            HistBar(TD.start + i * NS_PER_MIN, 100, 100.2, 99.8, 100, 500, 100, 5)
            for i in range(390)
        ]
    return ib


def make_runner(
    tmp_path: Path, clock: Clock, ib: FakeIb, cfg: LiveConfig | None = None
) -> LiveRunner:
    r = LiveRunner(cfg or config(), ib, UNIVERSE, tmp_path, tmp_path / "runs" / "live", clock)
    r.feed_timing = (0, 0)
    r.clock_sample_gap_s = 0
    return r


def drive(r: LiveRunner, ib: FakeIb, clock: Clock, until: int, price: float = 100.0) -> None:
    """每秒一笔 AAA 成交，每 100 毫秒推进一次。"""
    while clock.t < until:
        clock.t += 100 * 10**6
        if clock.t % S < 100 * 10**6:
            sec = clock.t // S * S - S
            ib.trade(TradeTick("AAA", sec, price, 100, clock.t))
        r.step(clock.t)


@pytest.fixture
def dataset(tmp_path: Path):
    return make_dataset(tmp_path, [PREV], symbols=("AAA", "BBB"))


def test_local_paper_end_to_end_and_recovery(tmp_path: Path, dataset):
    clock = Clock(T0)
    ib = fake_ib(clock)
    r = make_runner(tmp_path, clock, ib)
    asyncio.run(r.startup())
    assert ib.readonly is True and ib.port == 4002  # local_paper 只读连接
    assert not [a for a in r.alerts if a.kind in ("data_stale", "FEED_INTERRUPTED")]
    assert r.strategy_running is False  # 默认暂停
    assert r.start_strategy() is None and r.strategy_running
    # 策略的 1 分钟聚合器在 10:30 这一分钟用补数填过：10:31:00 收盘的 bar 是完整的一分钟
    drive(r, ib, clock, ny_to_ns(DAY, time(10, 32, 5)))
    strat = r.engine.strategy  # type: ignore[union-attr]
    assert isinstance(strat, BuyOnce)
    # 第一根是 10:29 的 1 分钟 bar：10:30:01（收盘 + 宽限）才可用，晚于启动时刻，所以要交付；
    # 它完全来自 IB 的 1 分钟补数（成交量 500），不是只有启动之后那部分的残缺 bar
    first = strat.bars[0]
    assert first.ts_start == ny_to_ns(DAY, time(10, 29)) and first.timeframe == "1m"
    assert first.volume == 500
    assert [b.ts_start for b in strat.bars[1:]] == [
        ny_to_ns(DAY, time(10, 30)),
        ny_to_ns(DAY, time(10, 31)),
    ]
    assert r.positions() == {"AAA": 10}
    orders = list(r.engine.oms.orders.values())  # type: ignore[union-attr]
    assert len(orders) == 1 and orders[0].status == OrderStatus.FILLED
    assert ib.tick_subs.keys() == {"AAA"} and ib.snap_subs.keys() == {"BBB"}
    r.close()
    # 重启恢复：持仓、订单、策略状态都从日志读回，策略仍是暂停
    clock2 = Clock(clock.t + 60 * S)
    ib2 = fake_ib(clock2)
    r2 = make_runner(tmp_path, clock2, ib2)
    asyncio.run(r2.startup())
    assert r2.positions() == {"AAA": 10}
    assert r2.engine.cfg.initial_state == {"entered": True}  # type: ignore[union-attr]
    assert any(a.kind == "recover" for a in r2.alerts)
    assert r2.strategy_running is False
    r2.close()


def test_feed_interruption_pauses_strategy_and_buttons(tmp_path: Path, dataset):
    clock = Clock(T0)
    ib = fake_ib(clock)
    r = make_runner(tmp_path, clock, ib)
    asyncio.run(r.startup())
    r.start_strategy()
    e = r.engine
    assert e is not None
    drive(r, ib, clock, ny_to_ns(DAY, time(10, 31, 5)))
    # AAA 超过 30 秒没有成交：看门狗报警并暂停策略
    stop = clock.t + 40 * S
    while clock.t < stop:
        clock.t += 500 * 10**6
        r.step(clock.t)
    assert "test_live_buy_once" in e.oms.halted
    assert any(a.kind == "FEED_INTERRUPTED" and a.symbol == "AAA" for a in r.alerts)
    # 暂停后策略不能开新仓（减仓照常）
    o = e.ctx.buy("AAA", 5, reason=Reason("x"))
    assert (
        e.oms.orders[o].status == OrderStatus.REJECTED and e.oms.orders[o].reject_rule == "halted"
    )
    # 人工平仓
    r.flatten_all()
    drive(r, ib, clock, clock.t + 5 * S, price=101.0)
    assert r.positions() == {}
    st = r.status()
    assert st["strategy_paused"] and st["positions"] == {} and "AAA" not in st["stale"]
    r.close()


def test_startup_refuses_live_account(tmp_path: Path, dataset):
    clock = Clock(T0)
    ib = fake_ib(clock, accounts=["U1234567"])
    r = make_runner(tmp_path, clock, ib)
    with pytest.raises(SafetyError, match="不是模拟账户"):
        asyncio.run(r.startup())
    assert not ib.connected  # 已断开


def test_startup_refuses_clock_skew(tmp_path: Path, dataset):
    clock = Clock(T0)
    ib = fake_ib(clock, server_skew_ns=5 * S)
    with pytest.raises(StartupError, match="相差 5.0 秒"):
        asyncio.run(make_runner(tmp_path, clock, ib).startup())


def test_requirements_check_blocks_snapshot_symbol(tmp_path: Path, dataset):
    """策略要求完整逐笔，而 BBB 只有快照：拒绝开启。"""

    class NeedTicks(BuyOnce):
        name = "test_live_need_ticks"
        requires = BuyOnce.requires.__class__(trades_complete=True)

    register_strategy(NeedTicks)
    clock = Clock(T0)
    cfg = config(strategy={"name": "test_live_need_ticks", "params": {"symbol": "BBB"}})
    r = make_runner(tmp_path, clock, fake_ib(clock), cfg)
    asyncio.run(r.startup())
    why = r.start_strategy()
    assert why is not None and "快照" in why and not r.strategy_running
    assert r.engine is not None and r.engine.strategy is None


def test_config_safety(tmp_path: Path):
    risk = RiskLimits().model_dump()
    with pytest.raises(ValueError, match="阶段 6 不支持实盘"):
        LiveConfig.model_validate({"profile": "live", "risk": risk})
    with pytest.raises(ValueError, match="4001"):
        LiveConfig.model_validate({"profile": "local_paper", "ibkr": {"port": 4001}, "risk": risk})
    del risk["max_daily_loss_usd"]
    p = tmp_path / "x.yaml"
    p.write_text(yaml.safe_dump({"profile": "local_paper", "risk": risk}), encoding="utf-8")
    with pytest.raises(ConfigError, match="max_daily_loss_usd"):
        load_live_config(p)


def test_shipped_local_paper_config_is_valid():
    root = Path(__file__).resolve().parents[1]
    cfg = load_live_config(root / "config" / "local_paper.yaml")
    assert cfg.profile == "local_paper" and cfg.ibkr.port == 4002 and cfg.feed.grace_ms == 1000


def test_cli_refuses_live(monkeypatch, capsys):
    from trader.apps import live_app

    monkeypatch.setattr("sys.argv", ["trader-live", "--profile", "live", "--confirm-live"])
    assert live_app.main() == 3
    assert "不支持实盘" in capsys.readouterr().out
