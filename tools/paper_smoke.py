"""IBKR 模拟账户下单冒烟测试（阶段 6 E，手工清单的自动部分）。

只连接模拟端口（4002），账户必须是 DU 开头的模拟账户；每张单只有 1 股 SPY。需要常规交易时段、
IB Gateway 已取消勾选 Read-Only API。步骤：

1. 远离市价的限价买单 → 券商接受 → 改价 → 改单确认 → 撤单 → 撤销确认
2. 市价买 1 股 → 成交、手续费到达 → 与券商对账一致
3. 原生括号单：可成交的限价买 1 股，止盈 +3%、止损 −3% → 入场成交，两张保护单在券商端工作
4. 人工平仓：撤销保护单 → 等撤单确认 → 市价卖出 → 持仓归零
5. 最终对账：本地与券商一致

用法：
    uv run python tools/paper_smoke.py               # 完整流程（常规时段）
    uv run python tools/paper_smoke.py --check-only  # 只连接、对账，不下单（任何时间）
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("TRADER_HOME", str(ROOT))

from trader.brokers.ibkr.api import IbAsyncApi  # noqa: E402
from trader.config import data_dir, load_universe  # noqa: E402
from trader.core.clock import WallClock  # noqa: E402
from trader.core.models import OrderIntent, OrderStatus, Reason  # noqa: E402
from trader.core.timeutil import from_ns  # noqa: E402
from trader.engine.engine import MANUAL  # noqa: E402
from trader.live.config import LiveConfig  # noqa: E402
from trader.live.runner import LiveRunner  # noqa: E402
from trader.oms.manager import round_price  # noqa: E402
from trader.oms.risk import RiskLimits  # noqa: E402

SYM = "SPY"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"  [{'通过' if ok else '失败'}] {name} {detail}", flush=True)
    return ok


async def main(check_only: bool) -> int:
    now = WallClock(lambda _e: None).now
    cfg = LiveConfig.model_validate({
        "profile": "broker_paper",
        "ibkr": {"port": 4002, "client_id": 23},
        "symbols": [SYM],
        "strategy": None,
        "feed": {"record": False},
        "risk": RiskLimits().model_dump(),
    })  # fmt: skip
    run_dir = ROOT / "runs" / "live" / from_ns(now()).date().isoformat() / "paper_smoke"
    runner = LiveRunner(cfg, IbAsyncApi(now), load_universe(), data_dir(), run_dir, now)
    runner.on_alert = lambda a: print(
        f"    {from_ns(a.ts):%H:%M:%S} {a.kind} {a.detail}", flush=True
    )
    await runner.startup()
    e = runner.engine
    assert e is not None
    if check_only:
        api = runner.api
        pos = [p for p in await api.positions() if p.qty]
        orders = await api.open_orders()
        execs = await api.executions()
        print(
            f"账户 {runner.account}：持仓 {[(p.symbol, p.qty, round(p.avg_cost, 2)) for p in pos]}"
        )
        print(f"券商挂单 {len(orders)} 张，当日成交 {len(execs)} 笔")
        mism = await runner.reconcile()
        print(f"对账（本测试只管理 {SYM}）：{mism or '一致'}")
        runner.close()
        return 0
    if e.oms.session.session != "regular":
        print("现在不是常规交易时段，市价单和括号单无法成交；请在 9:30–16:00 运行。")
        runner.close()
        return 2

    async def run_until(cond: Callable[[], bool], timeout: float = 20) -> bool:
        end = now() + int(timeout * 1e9)
        while now() < end:
            runner.step(now())
            if cond():
                return True
            await asyncio.sleep(0.1)
        return False

    def order(qty: int, side: str = "BUY", **kw) -> str:  # noqa: ANN003
        otype = kw.pop("order_type", "MKT")
        it = OrderIntent(source=MANUAL, symbol=SYM, side=side, qty=qty,  # type: ignore[arg-type]
                         order_type=otype, reason=Reason("paper_smoke"), **kw)  # fmt: skip
        return e.manual_order(it).client_order_id

    def st(coid: str) -> OrderStatus:
        return e.oms.orders[coid].status

    if not await run_until(lambda: SYM in e.portfolio.last_price, 20):
        print(f"20 秒内没有收到 {SYM} 的逐笔成交，不下单。请重启 IB Gateway 后再试。")
        runner.close()
        return 2
    last = e.portfolio.last_price[SYM]
    start_pos = e.portfolio.position(SYM).qty
    print(f"SPY 最新价 {last}，起始持仓 {start_pos}")

    print("1. 限价单：下单 → 改价 → 撤单")
    lo = round_price(last * 0.98)  # 风控限价偏离上限默认 3%
    a = order(1, order_type="LMT", limit_price=lo)
    check("券商接受", await run_until(lambda: st(a) == OrderStatus.ACCEPTED), str(lo))
    lo2 = round_price(last * 0.975)
    rej = e.oms.modify(a, now(), limit_price=lo2)
    ok = rej is None and await run_until(
        lambda: st(a) == OrderStatus.ACCEPTED and e.oms.orders[a].pending_intent is None
    )
    ok = ok and e.oms.orders[a].intent.limit_price == lo2
    check(
        "改单确认",
        ok,
        f"{e.oms.orders[a].intent.limit_price}" + (f" 被风控拒绝：{rej}" if rej else ""),
    )
    e.manual_cancel(a)
    check("撤单确认", await run_until(lambda: st(a) == OrderStatus.CANCELLED))

    print("2. 市价买 1 股")
    b = order(1)
    check(
        "成交",
        await run_until(lambda: st(b) == OrderStatus.FILLED),
        f"{e.oms.orders[b].avg_fill_price}",
    )
    ok = await run_until(lambda: not e.oms.orders[b].commission_pending, 30)
    check("手续费到达", ok, f"{e.oms.orders[b].commission}")
    mism = await runner.reconcile()
    check("对账一致", SYM not in mism, str(mism.get(SYM, "")))

    print("3. 原生括号单")
    c = order(1, order_type="LMT", limit_price=round_price(last * 1.003),
              take_profit=round_price(last * 1.03), stop_loss=round_price(last * 0.97))  # fmt: skip
    check("入场成交", await run_until(lambda: st(c) == OrderStatus.FILLED))
    kids = [o for o in e.oms.orders.values() if o.parent_id == c]
    ok = await run_until(lambda: all(k.status == OrderStatus.ACCEPTED for k in kids))
    check(
        "止盈止损在券商端工作", ok and len(kids) == 2, str([(k.role, k.status.value) for k in kids])
    )

    print("4. 人工平仓")
    runner.flatten_all()
    ok = await run_until(
        lambda: e.portfolio.position(SYM).qty == start_pos and not e.oms.open_orders(), 30
    )
    check(
        "撤销保护单并平仓",
        ok,
        f"持仓 {e.portfolio.position(SYM).qty}，挂单 {len(e.oms.open_orders())}",
    )

    print("5. 最终对账")
    await run_until(lambda: False, 3)
    mism = await runner.reconcile()
    check("本地与券商一致", SYM not in mism, str(mism))
    others = {k: v for k, v in mism.items() if k != SYM}
    if others:
        print(f"  （提示：模拟账户里还有本测试之外的持仓或挂单：{others}）")
    runner.close()
    failed = [n for n, ok, _ in results if not ok]
    print(
        f"\n结果：{len(results) - len(failed)}/{len(results)} 项通过"
        + (f"；失败：{failed}" if failed else "")
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main("--check-only" in sys.argv)))
