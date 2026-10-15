"""阶段 7：实盘启动检查（DESIGN.md 12.3 每项都有失败用例）、半自动确认（8.6）、亏损熔断（8.4）、
引擎心跳、发布检查。全部用假的 IB 客户端，永远不连接真实的 IBKR。"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
from tests.fake_ib import FakeIb

from conftest import make_dataset
from test_broker_paper import tick
from test_live_runner import PREV, T0, UNIVERSE, BuyOnce, Clock, config, fake_ib
from trader.brokers.ibkr.api import (
    BrokerPosition,
    IbAsyncApi,
    SafetyError,
    check_accounts,
    check_port,
)
from trader.core.models import Bar, OrderStatus, Reason
from trader.core.timeutil import NS_PER_SEC
from trader.live.config import GO_LIVE_ITEMS, LiveConfig, go_live_missing, load_live_config
from trader.live.control import execute
from trader.live.release import GitState, git_state, release_problems
from trader.live.runner import LiveRunner, StartupError
from trader.strategy.base import register_strategy, source_hash

S = NS_PER_SEC
GOOD_GIT = GitState(commit="abc123", tags=["V0.0.19"])


@register_strategy
class BuyBracketOnce(BuyOnce):
    """测试用：第一根 1 分钟 bar 买 10 股括号单（止盈 +2、止损 -2）。"""

    name = "test_live_bracket_once"

    def on_bar(self, bar: Bar) -> None:
        self.bars.append(bar)
        if not self.ctx.state.get("entered"):
            self.ctx.buy(
                bar.symbol, 10, reason=Reason("test_entry", "测试", {"ema": 99.5}),
                take_profit=bar.close + 2, stop_loss=bar.close - 2,
            )  # fmt: skip
            self.ctx.state["entered"] = True


@pytest.fixture
def dataset(tmp_path: Path):
    return make_dataset(tmp_path, [PREV], symbols=("AAA", "BBB"))


def live_cfg(**kw) -> LiveConfig:
    base = {
        "profile": "live",
        "ibkr": {"port": 4001, "account": "U1234567", "client_id": 31},
        "strategy": {
            "name": "test_live_buy_once",
            "params": {"symbol": "AAA"},
            "source_hash": source_hash(BuyOnce),
        },
        "go_live": dict.fromkeys(GO_LIVE_ITEMS, True),
    }
    return config(**(base | kw))


def runner(tmp_path: Path, cfg: LiveConfig, ib: FakeIb | None = None, clock: Clock | None = None):  # noqa: ANN201
    clock = clock or Clock(T0)
    ib = ib or fake_ib(clock)
    r = LiveRunner(cfg, ib, UNIVERSE, tmp_path, tmp_path / "runs" / cfg.profile, clock)
    r.feed_timing = (0, 0)
    r.clock_sample_gap_s = 0
    r.confirm_live = True
    r.git_state = lambda: GOOD_GIT
    return r, ib, clock


# ---------- 配置与连接安全规则 ----------


def test_live_config_rules():
    cfg = live_cfg()
    assert cfg.is_live and cfg.execution == "confirm"  # 实盘默认人工确认
    assert config(profile="broker_paper").execution == "auto"  # 模拟默认自动
    with pytest.raises(ValueError, match="实盘账户号"):
        live_cfg(ibkr={"port": 4001, "account": ""})
    with pytest.raises(ValueError, match="实盘账户号"):
        live_cfg(ibkr={"port": 4001, "account": "DU1234567"})
    with pytest.raises(ValueError, match="不认识"):
        live_cfg(go_live={"typo": True})
    with pytest.raises(ValueError, match="与运行方式"):
        config(profile="broker_paper", ibkr={"account": "U1234567"})
    auto = live_cfg(strategy={"name": "x", "source_hash": "h", "execution": "auto"})
    assert auto.execution == "auto"  # 只有显式写 auto 才自动执行
    assert go_live_missing(live_cfg(go_live={"account_limit": True})) == [
        t for k, t in GO_LIVE_ITEMS.items() if k != "account_limit"
    ]


def test_port_and_account_checks():
    check_port(4002)
    check_port(4001, live=True)
    with pytest.raises(SafetyError):
        check_port(4001)  # 模拟运行不能连实盘端口
    with pytest.raises(SafetyError):
        check_port(4002, live=True)  # 实盘不能连模拟端口
    check_accounts(["U1"], live=True)
    with pytest.raises(SafetyError, match="不是实盘账户"):
        check_accounts(["DU1"], live=True)
    with pytest.raises(SafetyError, match="不是模拟账户"):
        check_accounts(["U1"])


def test_real_api_refuses_port_before_connecting():
    """真实接口在连接之前就检查端口：模拟方式给实盘端口，根本不发起连接。"""
    api = IbAsyncApi(lambda: 0)
    with pytest.raises(SafetyError):
        asyncio.run(api.connect("127.0.0.1", 4001, 99, True))
    assert not api.is_connected()


# ---------- 实盘启动检查（DESIGN.md 12.3），每项一个失败用例 ----------


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda r: setattr(r, "confirm_live", False), "--confirm-live"),
        (lambda r: setattr(r.cfg, "go_live", {}), "上线清单未完成"),
        (
            lambda r: setattr(r, "git_state", lambda: GitState("abc", ["V1"], [" M src/x.py"])),
            "未提交的改动",
        ),
        (lambda r: setattr(r, "git_state", lambda: GitState("abc123", [])), "没有版本标签"),
        (lambda r: setattr(r.cfg.strategy, "source_hash", "0000"), "源码哈希"),
        (lambda r: setattr(r.cfg.strategy, "source_hash", ""), "配置登记的是 （空）"),
    ],
)
def test_live_preflight_failures(tmp_path: Path, dataset, change, message):
    r, ib, _ = runner(tmp_path, live_cfg())
    change(r)
    with pytest.raises(StartupError, match=message):
        asyncio.run(r.startup())
    assert ib.port is None  # 连接之前就拒绝了


def test_live_startup_refuses_paper_account(tmp_path: Path, dataset):
    clock = Clock(T0)
    r, ib, _ = runner(tmp_path, live_cfg(), fake_ib(clock, accounts=["DU1234567"]), clock)
    with pytest.raises(SafetyError, match="不是实盘账户"):
        asyncio.run(r.startup())
    assert not ib.connected


def test_live_startup_refuses_other_account(tmp_path: Path, dataset):
    clock = Clock(T0)
    r, ib, _ = runner(tmp_path, live_cfg(), fake_ib(clock, accounts=["U7654321"]), clock)
    with pytest.raises(SafetyError, match="不在已登录的账户"):
        asyncio.run(r.startup())


def test_live_startup_passes_with_everything_in_place(tmp_path: Path, dataset):
    async def main() -> None:
        clock = Clock(T0)
        r, ib, _ = runner(tmp_path, live_cfg(), fake_ib(clock, accounts=["U1234567"]), clock)
        await r.startup()
        assert ib.port == 4001 and ib.live is True and ib.readonly is False
        assert r.account == "U1234567"
        assert "test_live_buy_once" in r.engine.oms.confirm_sources  # type: ignore[union-attr]
        assert any("人工确认开仓" in a.detail for a in r.alerts)
        r.close()

    asyncio.run(main())


def test_shipped_live_example_config():
    root = Path(__file__).resolve().parents[1]
    cfg = load_live_config(root / "config" / "live.example.yaml")
    assert cfg.is_live and cfg.ibkr.port == 4001 and cfg.execution == "confirm"
    assert cfg.ui_port == 8766 and go_live_missing(cfg)  # 模板里的上线清单都没打勾


# ---------- 半自动确认（DESIGN.md 8.6） ----------


def confirm_runner(tmp_path: Path, strategy: str = "test_live_buy_once", **kw):  # noqa: ANN201
    cfg = config(
        profile="broker_paper",
        strategy={"name": strategy, "params": {"symbol": "AAA"}, "execution": "confirm"},
        **kw,
    )
    r, ib, clock = runner(tmp_path, cfg)
    return r, ib, clock


async def signal(r: LiveRunner, ib: FakeIb, clock: Clock) -> str:
    await r.startup()
    assert r.start_strategy() is None
    await tick(r, ib, clock, 2)
    (o,) = r.engine.oms.awaiting()  # type: ignore[union-attr]
    return o.client_order_id


def test_signal_waits_then_confirm_sends_once(tmp_path: Path, dataset):
    async def main() -> None:
        r, ib, clock = confirm_runner(tmp_path)
        coid = await signal(r, ib, clock)
        await tick(r, ib, clock, 3)
        assert ib.placed == []  # 没确认不发单
        assert any(a.kind == "SIGNAL" for a in r.alerts)
        st = r.status()
        (card,) = st["signals"]
        assert card["id"] == coid and card["qty"] == 10 and card["reason"] == "test_entry"
        assert card["notional"] == pytest.approx(1000) and card["expires"] > clock.t
        ok, _ = r.confirm_signal(coid)
        assert ok
        ok2, msg2 = r.confirm_signal(coid)  # 重复点击：不再发单
        assert not ok2 and "SUBMITTED" in msg2
        await tick(r, ib, clock)
        assert len(ib.placed) == 1 and ib.placed[0][1].order_ref == coid
        stats = r.signal_stats()
        assert stats["signals"] == 1 and stats["confirmed"] == 1 and stats["delay_max_s"] > 0
        r.close()

    asyncio.run(main())


def test_reject_and_timeout(tmp_path: Path, dataset):
    async def main() -> None:
        r, ib, clock = confirm_runner(tmp_path)
        coid = await signal(r, ib, clock)
        e = r.engine
        assert e is not None
        ok, _ = r.reject_signal(coid)
        assert ok and e.oms.orders[coid].status == OrderStatus.REJECTED
        assert e.oms.orders[coid].reject_rule == "operator_reject"
        assert not r.confirm_signal(coid)[0]  # 拒绝后再确认：不发
        # 再来一个信号，不确认，等超时
        e.ctx.state["entered"] = False
        await tick(r, ib, clock, 80)  # 下一根 1 分钟 bar 收盘出信号，15 秒后超时
        expired = [o for o in e.oms.orders.values() if o.status == OrderStatus.EXPIRED]
        assert expired and expired[0].reject_rule == "confirm_timeout"
        strat = e.strategy
        assert isinstance(strat, BuyOnce)
        assert ib.placed == []
        stats = r.signal_stats()
        assert stats["rejected"] == 1 and stats["expired"] >= 1 and stats["confirmed"] == 0
        r.close()

    asyncio.run(main())


def test_confirm_rechecks_risk_and_price(tmp_path: Path, dataset):
    async def main() -> None:
        r, ib, clock = confirm_runner(tmp_path)
        coid = await signal(r, ib, clock)
        e = r.engine
        assert e is not None
        await tick(r, ib, clock, 2, price=101.0)  # 价格偏离 1%，超过 0.5%
        ok, msg = r.confirm_signal(coid)
        assert not ok and "confirm_price_move" in msg
        assert e.oms.orders[coid].status == OrderStatus.REJECTED
        # 停止新开仓后确认：作废
        e.ctx.state["entered"] = False
        await tick(r, ib, clock, 60, price=101.0)
        (o2,) = e.oms.awaiting()
        r.halt_new(True)
        ok, msg = r.confirm_signal(o2.client_order_id)
        assert not ok and "halted" in msg
        assert ib.placed == []
        r.close()

    asyncio.run(main())


def test_cancel_while_waiting_is_local(tmp_path: Path, dataset):
    async def main() -> None:
        r, ib, clock = confirm_runner(tmp_path)
        coid = await signal(r, ib, clock)
        r.cancel_all()
        await tick(r, ib, clock)
        assert r.engine.oms.orders[coid].status == OrderStatus.CANCELLED  # type: ignore[union-attr]
        assert ib.cancels == [] and ib.placed == []
        r.close()

    asyncio.run(main())


def test_exits_and_manual_orders_skip_confirmation(tmp_path: Path, dataset):
    async def main() -> None:
        r, ib, clock = confirm_runner(tmp_path)
        coid = await signal(r, ib, clock)
        r.confirm_signal(coid)
        await tick(r, ib, clock)
        (oid, _), = ib.placed  # fmt: skip
        ib.execute(oid, 10, 100.0, "e1.01.01", commission=1.0)
        await tick(r, ib, clock)
        e = r.engine
        assert e is not None
        assert r.positions() == {"AAA": 10}
        # 策略自己的减仓单：不需要确认，直接发出
        e.ctx.close_position("AAA", reason=Reason("test_exit"))
        await tick(r, ib, clock)
        assert len(ib.placed) == 2 and not e.oms.awaiting()
        assert e.oms.confirm_sources == {"test_live_buy_once"}  # 人自己下的手动单不需要确认
        r.close()

    asyncio.run(main())


def test_bracket_children_created_only_after_confirm(tmp_path: Path, dataset):
    async def main() -> None:
        r, ib, clock = confirm_runner(tmp_path, strategy="test_live_bracket_once")
        coid = await signal(r, ib, clock)
        e = r.engine
        assert e is not None
        assert [o for o in e.oms.orders.values() if o.parent_id] == []
        card = r.status()["signals"][0]
        assert card["take_profit"] and card["stop_loss"] and card["context"] == {"ema": 99.5}
        r.confirm_signal(coid)
        await tick(r, ib, clock)
        assert len(ib.placed) == 3  # 入场 + 止盈 + 止损一起发出
        r.close()

    asyncio.run(main())


def test_restart_voids_waiting_signals(tmp_path: Path, dataset):
    async def main() -> None:
        r, ib, clock = confirm_runner(tmp_path)
        coid = await signal(r, ib, clock)
        await tick(r, ib, clock)
        r.close()
        r2, ib2, _ = runner(tmp_path, r.cfg, fake_ib(clock), clock)
        await r2.startup()
        o = r2.engine.oms.orders[coid]  # type: ignore[union-attr]
        assert o.status == OrderStatus.CANCELLED and ib2.placed == []
        assert not r2.confirm_signal(coid)[0]
        r2.close()

    asyncio.run(main())


def test_control_commands_for_signals(tmp_path: Path, dataset):
    async def main() -> None:
        r, ib, clock = confirm_runner(tmp_path)
        coid = await signal(r, ib, clock)
        ok, _ = await execute(r, {"cmd": "confirm_signal", "id": coid})
        assert ok
        ok, msg = await execute(r, {"cmd": "reject_signal", "id": coid})
        assert not ok and "SUBMITTED" in msg
        ok, msg = await execute(r, {"cmd": "confirm_signal", "id": "nope"})
        assert not ok and "没有信号" in msg
        r.close()

    asyncio.run(main())


# ---------- 亏损熔断、心跳 ----------


def test_daily_loss_circuit_breaker(tmp_path: Path, dataset):
    async def main() -> None:
        cfg = config(profile="broker_paper")
        r, ib, clock = runner(tmp_path, cfg)
        await r.startup()
        assert r.start_strategy() is None
        await tick(r, ib, clock, 2)  # 策略买 10 股（自动执行）
        ((oid, _),) = ib.placed
        ib.execute(oid, 10, 100.0, "e1.01.01", commission=1.0)
        ib.broker_positions = [BrokerPosition("DU1234567", "AAA", 10, 100.0)]
        await tick(r, ib, clock)
        assert not r.loss_tripped
        limit = cfg.risk.max_daily_loss_usd
        await tick(r, ib, clock, 2, price=100.0 - limit / 10 - 1)  # 每股跌超过 200 美元
        e = r.engine
        assert e is not None
        assert r.loss_tripped and e.oms.halt_all_new
        assert "test_live_buy_once" in e.oms.halted
        assert [a for a in r.alerts if a.kind == "RISK_TRIGGERED"]
        assert r.positions() == {"AAA": 10}  # 不自动平仓
        assert r.status()["loss_tripped"]
        r.close()

    asyncio.run(main())


def test_status_reports_main_loop_time(tmp_path: Path, dataset):
    async def main() -> None:
        r, ib, clock = runner(tmp_path, config(profile="broker_paper"))
        await r.startup()
        await tick(r, ib, clock)
        assert r.status()["loop_ts"] == clock.t
        r.close()

    asyncio.run(main())


# ---------- 发布检查 ----------


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)  # noqa: S603, S607


def test_git_release_state(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("1", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "c1")
    assert release_problems(git_state(repo)) and "版本标签" in release_problems(git_state(repo))[0]
    _git(repo, "tag", "V9.9.9")
    assert release_problems(git_state(repo)) == []
    (repo / "a.txt").write_text("2", encoding="utf-8")
    assert "未提交的改动" in release_problems(git_state(repo))[0]
    assert "无法读取" in release_problems(git_state(tmp_path / "missing"))[0]


def test_release_dir_files_do_not_make_tree_dirty(tmp_path: Path):
    """发布目录里生成的 run_live.bat 和 config/live.yaml 被 .gitignore 忽略，不算未提交的改动。"""
    root = Path(__file__).resolve().parents[1]
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / ".gitignore").write_text(
        (root / ".gitignore").read_text(encoding="utf-8"), encoding="utf-8"
    )
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "c1")
    _git(repo, "tag", "V1")
    (repo / "run_live.bat").write_text("x", encoding="utf-8")
    (repo / "config" / "live.yaml").write_text("x", encoding="utf-8")
    (repo / "runs").mkdir()
    (repo / "runs" / "x.txt").write_text("x", encoding="utf-8")
    assert release_problems(git_state(repo)) == []


# ---------- 界面 ----------


def test_live_window_signals_and_heartbeat(qtbot, tmp_path: Path, dataset):
    from trader.gui.live_window import LiveWindow

    async def make_status() -> dict:
        r, ib, clock = confirm_runner(tmp_path, strategy="test_live_bracket_once")
        await signal(r, ib, clock)
        st = r.status()
        r.close()
        return st

    st = asyncio.run(make_status())
    w = LiveWindow("live", 1)
    qtbot.addWidget(w)
    notes: list[tuple[str, str]] = []
    w.notify = lambda t, x: notes.append((t, x))
    assert w.windowTitle().startswith("【实盘 真实资金】")
    w._clock.now = lambda: st["ts"]  # type: ignore[method-assign]
    w.apply_status(st)
    assert w.signals.model().rowCount() == 1
    assert "1 条" in w.signal_group.title() and w.btn_confirm.isEnabled()
    assert "人工确认开仓" in w.strategy_label.text()
    w.sock.state = lambda: w.sock.SocketState.ConnectedState  # type: ignore[method-assign]
    w.sock.write = lambda _b: 0  # type: ignore[method-assign]
    (coid,) = w.selected_signals()  # 只有一条：不选也算选中
    w._answer("confirm_signal")
    assert w.sent[-1]["cmd"] == "confirm_signal" and w.sent[-1]["id"] == coid
    # 主循环卡住：状态还在来，但 loop_ts 停在 10 秒前
    w._clock.now = lambda: st["ts"] + 10 * S  # type: ignore[method-assign]
    w.last_status_at = (st["ts"] + 10 * S) / 1e9
    w._tick()
    assert "主循环" in w.health.text() and notes and "无响应" in notes[-1][0]
    w.grab().save(str(tmp_path / "live_signals.png"))
