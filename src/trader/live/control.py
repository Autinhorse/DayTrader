"""实盘版的本机控制接口（DESIGN.md 11、12.4）：引擎和界面是两个进程，界面关闭不影响交易。

- 只监听 127.0.0.1（配置成其他地址时拒绝），每行一条 JSON。
- 引擎 → 界面：连接时先发最近的告警；之后每秒一条 {"type": "status", ...}，
  每条告警 {"type": "alert", ...}，
  每个命令的结果 {"type": "result", "id": ..., "ok": ..., "msg": ...}。
- 界面 → 引擎：{"id": 1, "cmd": "start_strategy", ...}。命令都经过 LiveRunner 的同一套方法，
  手动下单与策略订单走同一条路径（风控、落盘、发件箱）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from decimal import Decimal
from typing import Any

from trader.core.models import OrderIntent, Reason
from trader.engine.engine import MANUAL
from trader.live.runner import Alert, LiveRunner
from trader.oms.manager import round_price

LOOPBACK = "127.0.0.1"
ALERT_HISTORY = 300


def alert_msg(a: Alert) -> dict[str, Any]:
    return {"type": "alert", "ts": a.ts, "kind": a.kind, "detail": a.detail, "symbol": a.symbol}


def _px(x: Any) -> Decimal | None:
    return None if x in (None, "", 0) else round_price(float(x))


async def execute(runner: LiveRunner, cmd: dict[str, Any]) -> tuple[bool, str]:
    """执行一条界面命令，返回 (是否成功, 说明)。"""
    name = cmd.get("cmd")
    e = runner.engine
    if e is None:
        return False, "引擎还在启动"
    if name == "start_strategy":
        why = runner.start_strategy(force=bool(cmd.get("force")))
        return (why is None), (why or "策略已开启")
    if name == "pause_strategy":
        runner.pause_strategy("界面操作")
        return True, "策略已暂停（只能减仓）"
    if name in ("stop_strategy", "stop_all_strategies"):
        runner.stop_strategy()
        return True, "策略已停止，持仓转为手动"
    if name == "halt_new":
        on = bool(cmd.get("on", True))
        runner.halt_new(on)
        return True, "已停止新开仓" if on else "已恢复新开仓"
    if name == "cancel_all":
        runner.cancel_all()
        return True, "已发出全部撤单"
    if name == "flatten_all":
        runner.flatten_all()
        return True, "已开始平仓（先撤单，确认后平仓）"
    if name == "cancel_order":
        coid = str(cmd.get("id", ""))
        if coid not in e.oms.orders:
            return False, f"没有订单 {coid}"
        e.manual_cancel(coid)
        return True, f"已发出撤单 {coid}"
    if name == "manual_order":
        return _manual_order(runner, cmd)
    if name == "reconcile":
        found = await runner.reconcile()
        return True, (
            "对账一致"
            if not found
            else "不一致：" + "；".join(f"{k} {v}" for k, v in found.items())
        )
    if name == "resync_to_broker":
        if runner.executor is None:
            return False, "local_paper 没有券商持仓可以对照"
        pos = {
            p.symbol: (int(round(p.qty)), p.avg_cost)
            for p in await runner.api.positions()
            if p.account == runner.account and p.qty
        }
        runner.resync_to_broker(pos)
        return True, f"已按券商持仓重置：{pos or '空仓'}"
    if name == "shutdown":
        runner.stop()
        return True, "引擎将在当前循环结束后退出"
    return False, f"未知命令 {name}"


def _manual_order(runner: LiveRunner, cmd: dict[str, Any]) -> tuple[bool, str]:
    e = runner.engine
    assert e is not None
    try:
        intent = OrderIntent(
            source=MANUAL,
            symbol=str(cmd["symbol"]).upper(),
            side=str(cmd["side"]).upper(),  # type: ignore[arg-type]
            qty=int(cmd["qty"]),
            order_type=str(cmd.get("type", "MKT")).upper(),  # type: ignore[arg-type]
            reason=Reason("manual", str(cmd.get("note", "界面手动下单"))),
            limit_price=_px(cmd.get("limit")),
            stop_price=_px(cmd.get("stop")),
            outside_rth=bool(cmd.get("outside_rth", False)),
            take_profit=_px(cmd.get("take_profit")),
            stop_loss=_px(cmd.get("stop_loss")),
        )
    except (KeyError, ValueError, TypeError) as exc:
        return False, f"订单参数不对：{exc}"
    if intent.symbol not in e.cfg.whitelist:
        return False, f"{intent.symbol} 不在清单里"
    try:
        order = e.manual_order(intent)
    except ValueError as exc:  # 标的被策略占用等
        return False, str(exc)
    if order.reject_rule:
        return False, f"风控拒绝（{order.reject_rule}）：{order.reject_detail}"
    return True, f"已下单 {order.client_order_id}"


class ControlServer:
    def __init__(self, runner: LiveRunner, host: str, port: int) -> None:
        if host != LOOPBACK:
            raise ValueError(f"控制接口只允许监听 {LOOPBACK}（配置的是 {host}）")
        self.runner, self.host, self.port = runner, host, port
        self._clients: set[asyncio.StreamWriter] = set()
        self._server: asyncio.AbstractServer | None = None
        self._ticker: asyncio.Task | None = None
        prev = runner.on_alert

        def on_alert(a: Alert) -> None:
            if prev is not None:
                prev(a)
            self.broadcast(alert_msg(a))

        runner.on_alert = on_alert

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        self._ticker = asyncio.ensure_future(self._tick())
        return self.port

    async def close(self) -> None:
        if self._ticker is not None:
            self._ticker.cancel()
        for w in list(self._clients):
            w.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def broadcast(self, msg: dict[str, Any]) -> None:
        line = (json.dumps(msg, ensure_ascii=False, default=str) + "\n").encode("utf-8")
        for w in list(self._clients):
            try:
                w.write(line)
            except (ConnectionError, RuntimeError):
                self._clients.discard(w)

    async def _tick(self) -> None:
        while True:
            self.broadcast({"type": "status", **self.runner.status()})
            await asyncio.sleep(1)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._clients.add(writer)
        for a in self.runner.alerts[-ALERT_HISTORY:]:
            writer.write((json.dumps(alert_msg(a), ensure_ascii=False) + "\n").encode("utf-8"))
        writer.write((json.dumps({"type": "status", **self.runner.status()}, ensure_ascii=False,
                                 default=str) + "\n").encode("utf-8"))  # fmt: skip
        try:
            while line := await reader.readline():
                try:
                    cmd = json.loads(line)
                    ok, msg = await execute(self.runner, cmd)
                except Exception as exc:  # noqa: BLE001 - 命令出错不能让引擎停下
                    cmd, ok, msg = {}, False, f"命令执行出错：{exc}"
                self.runner._alert(self.runner.now(), "command", f"{cmd.get('cmd')}：{msg}")
                reply = {"type": "result", "id": cmd.get("id"), "ok": ok, "msg": msg}
                writer.write((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
                self.broadcast({"type": "status", **self.runner.status()})
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self._clients.discard(writer)
            with contextlib.suppress(Exception):
                writer.close()
