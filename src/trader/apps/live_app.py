"""实盘版入口：trader-live（DESIGN.md 12.1）。不得导入 trader.research。

    uv run trader-live --profile local_paper                    # 实时行情 + 本地模拟成交
    uv run trader-live --profile local_paper --start-strategy   # 同时开启配置里的策略
    uv run trader-live --profile local_paper --minutes 10       # 只运行 10 分钟（试运行）
    uv run trader-live --profile broker_paper --ui              # 同时打开界面（独立进程）

阶段 6：只允许 local_paper 和 broker_paper（IBKR 模拟账户，DU 开头）；live 一律拒绝。
    uv run trader-live --profile broker_paper                   # IBKR 模拟账户下单
运行状态每分钟写入 runs/live/<日期>/<profile>/status.json，提醒和告警同时打印在窗口里。
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path

from trader.brokers.ibkr.api import IbAsyncApi, SafetyError
from trader.config import data_dir, load_universe, project_dir
from trader.core.clock import WallClock
from trader.core.timeutil import NS_PER_MIN, from_ns
from trader.indicators.base import load_all
from trader.live.config import ConfigError, load_live_config
from trader.live.control import ControlServer
from trader.live.runner import Alert, LiveRunner, StartupError
from trader.strategy.base import load_user_strategies


def _print_alert(a: Alert) -> None:
    sym = f" [{a.symbol}]" if a.symbol else ""
    print(f"{from_ns(a.ts):%H:%M:%S} {a.kind}{sym} {a.detail}", flush=True)


async def _main(args: argparse.Namespace) -> int:
    root = project_dir()
    try:
        cfg = load_live_config(root / "config" / f"{args.profile}.yaml")
    except (ConfigError, FileNotFoundError) as exc:
        print(f"配置错误：{exc}")
        return 2
    errors = load_all(root / "user" / "indicators") | load_user_strategies(
        root / "user" / "strategies"
    )
    if errors:
        print("加载用户指标或策略失败：" + "；".join(f"{k}: {v}" for k, v in errors.items()))
        return 2
    now = WallClock(lambda _ev: None).now
    today = from_ns(now()).date().isoformat()
    runner = LiveRunner(
        cfg,
        IbAsyncApi(now),
        load_universe(),
        data_dir(),
        root / "runs" / "live" / today / cfg.profile,
        now,
    )
    runner.on_alert = _print_alert
    control = ControlServer(runner, cfg.control_host, cfg.ui_port)
    try:
        port = await control.start()
    except OSError as exc:
        print(f"控制端口 {cfg.ui_port} 被占用（同一运行方式已经在运行？）：{exc}")
        return 3
    print(f"界面连接端口 127.0.0.1:{port}；打开界面：uv run trader-live-ui --profile {cfg.profile}")
    if args.ui:
        _launch_ui(cfg.profile)
    try:
        await runner.startup()
        if args.start_strategy:
            why = runner.start_strategy(force=args.force)
            if why:
                print(f"策略没有开启：{why}")
        until = now() + args.minutes * NS_PER_MIN if args.minutes else None
        await runner.run(until)
    except (SafetyError, StartupError) as exc:
        print(f"拒绝启动：{exc}")
        return 3
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("已手动结束。")
    finally:
        st = runner.status()
        pos = {s: p["qty"] for s, p in st.get("positions", {}).items()}
        print(
            f"结束：持仓 {pos}，挂单 {len(st.get('open_orders', []))} 张，"
            f"成交 {len(st.get('fills', []))} 笔，迟到 bar {st.get('late_bars', 0)}，"
            f"迟到修订 {st.get('late_revisions', 0)}"
        )
        await control.close()
        runner.close()
    return 0


def _launch_ui(profile: str) -> None:
    """界面是独立进程：关掉界面不影响引擎。"""
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")  # Windows 上不弹出多余的控制台窗口
    subprocess.Popen(  # noqa: S603
        [str(pyw if pyw.exists() else exe), "-m", "trader.apps.live_ui", "--profile", profile],
        creationflags=flags,
    )


def main() -> int:
    p = argparse.ArgumentParser(description="DayTrader 实盘版（阶段 6：只允许模拟）")
    p.add_argument("--profile", required=True, choices=["local_paper", "broker_paper", "live"])
    p.add_argument("--start-strategy", action="store_true", help="启动后开启配置里的策略")
    p.add_argument("--force", action="store_true", help="数据有缺口时也开启策略")
    p.add_argument("--minutes", type=float, help="只运行这么多分钟（默认到当天盘后结束）")
    p.add_argument("--ui", action="store_true", help="同时打开实盘版界面（独立窗口）")
    p.add_argument("--confirm-live", action="store_true", help=argparse.SUPPRESS)
    args = p.parse_args()
    if args.profile == "live":
        print("拒绝启动：阶段 6 不支持实盘（live）；实盘接入在阶段 7。")
        return 3
    try:
        return asyncio.run(_main(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
