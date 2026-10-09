"""实盘版控制接口与界面：命令执行、本机端口协议、界面显示与按钮（不连接 IBKR，不需要引擎进程）。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from conftest import make_dataset
from test_live_runner import PREV, T0, UNIVERSE, Clock, config, drive, fake_ib
from trader.live.control import ControlServer, execute
from trader.live.runner import LiveRunner


@pytest.fixture
def dataset(tmp_path: Path):
    return make_dataset(tmp_path, [PREV], symbols=("AAA", "BBB"))


def make(tmp_path: Path, **kw):  # noqa: ANN201
    clock = Clock(T0)
    ib = fake_ib(clock)
    r = LiveRunner(config(**kw), ib, UNIVERSE, tmp_path, tmp_path / "runs" / "ctl", clock)
    r.feed_timing = (0, 0)
    r.clock_sample_gap_s = 0
    return r, ib, clock


def test_commands(tmp_path: Path, dataset):
    async def main() -> None:
        r, ib, clock = make(tmp_path)
        assert await execute(r, {"cmd": "halt_new"}) == (False, "引擎还在启动")
        await r.startup()
        ok, msg = await execute(
            r, {"cmd": "manual_order", "symbol": "aaa", "side": "buy", "qty": 10}
        )
        assert ok and msg.startswith("已下单 manual-")
        ok, msg = await execute(
            r, {"cmd": "manual_order", "symbol": "ZZZ", "side": "BUY", "qty": 1}
        )
        assert not ok and "不在清单" in msg
        ok, msg = await execute(
            r, {"cmd": "manual_order", "symbol": "AAA", "side": "BUY", "qty": 999999}
        )
        assert not ok and "风控拒绝" in msg
        ok, msg = await execute(r, {"cmd": "manual_order", "symbol": "AAA", "side": "BUY"})
        assert not ok and "参数" in msg
        drive(r, ib, clock, clock.t + 3 * 10**9)
        assert r.positions() == {"AAA": 10}
        lim = await execute(r, {"cmd": "manual_order", "symbol": "AAA", "side": "SELL", "qty": 10,
                                "type": "LMT", "limit": 102.5})  # fmt: skip
        coid = lim[1].split()[-1]
        assert (await execute(r, {"cmd": "cancel_order", "id": coid}))[0]
        assert not (await execute(r, {"cmd": "cancel_order", "id": "nope"}))[0]
        assert (await execute(r, {"cmd": "halt_new", "on": True}))[0] and r.engine.oms.halt_all_new  # type: ignore[union-attr]
        assert (await execute(r, {"cmd": "halt_new", "on": False}))[0]
        ok, msg = await execute(r, {"cmd": "start_strategy"})  # AAA 被手动持仓占用
        assert not ok and "manual" in msg and not r.strategy_running
        r.engine.oms.owners["AAA"] = "test_live_buy_once"  # type: ignore[union-attr]
        assert (await execute(r, {"cmd": "start_strategy"}))[0] and r.strategy_running
        assert (await execute(r, {"cmd": "pause_strategy"}))[0]
        assert (await execute(r, {"cmd": "stop_strategy"}))[0] and not r.strategy_running
        assert (await execute(r, {"cmd": "flatten_all"}))[0]
        drive(r, ib, clock, clock.t + 3 * 10**9)
        assert r.positions() == {}
        ok, msg = await execute(r, {"cmd": "resync_to_broker"})
        assert not ok and "local_paper" in msg
        assert await execute(r, {"cmd": "bogus"}) == (False, "未知命令 bogus")
        r.close()

    asyncio.run(main())


def test_status_is_json_serializable(tmp_path: Path, dataset):
    async def main() -> None:
        r, ib, clock = make(tmp_path)
        await r.startup()
        await execute(r, {"cmd": "manual_order", "symbol": "AAA", "side": "BUY", "qty": 10})
        drive(r, ib, clock, clock.t + 3 * 10**9)
        st = json.loads(json.dumps(r.status()))
        assert st["ready"] and st["positions"]["AAA"]["qty"] == 10
        assert st["orders"][0]["status"] == "FILLED" and st["fills"][0]["qty"] == 10
        assert st["symbols"] == ["AAA", "BBB"] and st["session"] == "regular"
        r.close()

    asyncio.run(main())


def test_socket_protocol(tmp_path: Path, dataset):
    async def main() -> None:
        r, ib, clock = make(tmp_path)
        with pytest.raises(ValueError, match="只允许监听"):
            ControlServer(r, "0.0.0.0", 0)  # noqa: S104
        server = ControlServer(r, "127.0.0.1", 0)
        port = await server.start()
        await r.startup()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        msgs = []
        while True:  # 先收到历史告警，再收到一条状态
            m = json.loads(await reader.readline())
            msgs.append(m)
            if m["type"] == "status":
                break
        assert any(m["type"] == "alert" and m["kind"] == "startup" for m in msgs)
        writer.write(b'{"id": 7, "cmd": "halt_new", "on": true}\n')
        await writer.drain()
        result = None
        for _ in range(20):
            m = json.loads(await asyncio.wait_for(reader.readline(), 3))
            if m["type"] == "result":
                result = m
                break
        assert result == {"type": "result", "id": 7, "ok": True, "msg": "已停止新开仓"}
        writer.write(b"not json\n")
        await writer.drain()
        for _ in range(20):
            m = json.loads(await asyncio.wait_for(reader.readline(), 3))
            if m["type"] == "result":
                assert not m["ok"] and "出错" in m["msg"]
                break
        writer.close()
        await server.close()
        r.close()

    asyncio.run(main())


# ---------- 界面 ----------


STATUS = {
    "type": "status", "profile": "broker_paper", "ready": True, "connected": True,
    "account": "DU1234567", "session": "regular", "strategy": "orb_breakout",
    "strategy_running": True, "strategy_paused": True, "halt_all_new": False,
    "equity": 100123.4, "day_pnl": 123.4, "realized": 100.0, "unrealized": 23.4, "commission": 2.0,
    "positions": {"NVDA": {"qty": -42, "avg_cost": 232.49, "last": 231.9, "unrealized": 24.8,
                           "owner": "orb_breakout"}},
    "open_orders": [{"id": "orb-1", "symbol": "NVDA", "side": "BUY", "qty": 42, "type": "STP",
                     "limit": None, "stop": 235.0, "filled": 0, "avg": None, "status": "ACCEPTED",
                     "role": "stop_loss", "source": "orb_breakout", "reason": "stop_loss",
                     "reject": ""}],
    "orders": [], "fills": [{"ts": 1, "id": "orb-0", "symbol": "NVDA", "side": "SELL", "qty": 42,
                             "price": 232.49, "exec_id": "e1"}],
    "queued": 0, "mismatch": {}, "stale": ["KORU"], "gaps": [], "symbols": ["KORU", "NVDA"],
    "last_price": {"NVDA": 231.9}, "last_bar": {"NVDA": "13:05:01"},
}  # fmt: skip


def test_live_window(qtbot):
    from trader.gui.live_window import LiveWindow

    w = LiveWindow("broker_paper", 1)  # 端口 1：连不上，命令只记录不发送
    qtbot.addWidget(w)
    notes: list[tuple[str, str]] = []
    w.notify = lambda t, x: notes.append((t, x))
    w.confirm = lambda text: True
    w.handle(STATUS)
    assert "引擎正常" in w.health.text() and "行情中断 KORU" in w.health.text()
    assert "已暂停" in w.strategy_label.text()
    assert w.positions.visible_count() == 1 and w.open_orders.visible_count() == 1
    assert w.fills.visible_count() == 1 and w.market.visible_count() == 2
    assert "IBKR 模拟账户" in w.banner.text()
    w.handle({"type": "alert", "ts": 1, "kind": "RECONCILE_MISMATCH", "detail": "AAA 不一致"})
    w.handle({"type": "alert", "ts": 1, "kind": "startup", "detail": "启动完成"})
    assert notes == [("DayTrader RECONCILE_MISMATCH", "AAA 不一致")]
    assert w.alerts.count() == 2
    # 按钮 → 命令
    w.btn_start.click()
    w.btn_pause.click()
    w.btn_halt.click()
    w.o_symbol.setCurrentText("nvda")
    w.o_qty.setValue(5)
    w.o_type.setCurrentText("LMT")
    w.o_limit.setValue(230.5)
    w._submit_order()
    cmds = [m["cmd"] for m in w.sent]
    assert cmds == ["start_strategy", "pause_strategy", "halt_new", "manual_order"]
    order = w.sent[-1]
    assert (order["symbol"], order["qty"], order["type"], order["limit"]) == (
        "NVDA",
        5,
        "LMT",
        230.5,
    )
    assert order["stop"] is None and order["take_profit"] is None
    # 取消确认时不发命令
    w.confirm = lambda text: False
    n = len(w.sent)
    w._submit_order()
    assert len(w.sent) == n
    # 心跳：超过 5 秒没有状态
    w.last_status_at = w._clock.now() / 1e9 - 10
    w._tick()
    assert "引擎无响应" in w.health.text()
    # 引擎重启中
    w.handle({"type": "status", "profile": "broker_paper", "ready": False})
    assert "正在启动" in w.health.text()
    w.handle({"type": "result", "id": 1, "ok": False, "msg": "风控拒绝"})
    assert "风控拒绝" in w.statusBar().currentMessage()
