"""实盘发布（DESIGN.md 12.5）：把一个版本标签检出到单独的目录，给实盘版独立的代码、虚拟环境、
配置和运行数据库。研究目录里改代码、切分支、装依赖都碰不到正在运行的交易进程。

    uv run python tools/release_live.py                 # 发布当前提交的版本标签
    uv run python tools/release_live.py --tag V0.0.19   # 发布指定标签

做的事情（不连接 IBKR，不下单）：
1. git worktree 把标签检出到 ..\\DayTrader-live\\<标签>（已存在就停下，不覆盖）；
2. 在那里建独立的虚拟环境，只装核心依赖和实盘依赖
   （uv sync --frozen --no-default-groups --extra live）；
3. config/live.yaml：从上一次发布复制；第一次发布从 config/live.example.yaml 复制，需要你填写；
4. 生成 run_live.bat（红色窗口，双击启动实盘引擎和界面）。历史数据仍读研究目录的 data/。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

BAT = """@echo off
rem DayTrader LIVE (real money), release {tag}: engine + UI.
rem Strategy entries need manual confirmation in the UI.
cd /d "%~dp0"
color 4F
set TRADER_DATA_DIR={data}
start "DayTrader LIVE {tag}" .venv\\Scripts\\python.exe -m trader.apps.live_app ^
 --profile live --confirm-live --ui
"""


def run(*args: str, cwd: Path = ROOT) -> str:
    print("  >", " ".join(args))
    out = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8")  # noqa: S603
    if out.returncode != 0:
        raise SystemExit(f"失败：{' '.join(args)}\n{out.stdout}{out.stderr}")
    return out.stdout


def previous_config(base: Path, dest: Path) -> Path | None:
    """最近一次发布（按修改时间）里的 config/live.yaml。"""
    found = [
        p / "config" / "live.yaml"
        for p in base.iterdir()
        if p.is_dir() and p != dest and (p / "config" / "live.yaml").exists()
    ]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def write_bat(dest: Path, tag: str, data: Path) -> Path:
    bat = dest / "run_live.bat"
    bat.write_text(BAT.format(tag=tag, data=data), encoding="ascii", newline="\r\n")
    return bat


def main() -> int:
    p = argparse.ArgumentParser(description="把版本标签发布到单独的实盘目录")
    p.add_argument("--tag", help="版本标签（默认：当前提交上的标签）")
    p.add_argument("--base", type=Path, default=ROOT.parent / "DayTrader-live", help="发布根目录")
    args = p.parse_args()
    tag = args.tag
    if not tag:
        tags = run("git", "tag", "--points-at", "HEAD").split()
        if not tags:
            print("当前提交没有版本标签；先打标签，或用 --tag 指定。")
            return 2
        tag = tags[-1]
    run("git", "rev-parse", "--verify", f"refs/tags/{tag}")
    dest = args.base / tag
    if dest.exists():
        print(f"{dest} 已存在，不覆盖。要重新发布先删除它（git worktree remove {dest}）。")
        return 2
    args.base.mkdir(parents=True, exist_ok=True)
    print(f"1. 检出 {tag} → {dest}")
    run("git", "worktree", "add", "--detach", str(dest), tag)
    print("2. 建立独立的虚拟环境（只装核心依赖和实盘依赖）")
    run("uv", "sync", "--frozen", "--no-default-groups", "--extra", "live", cwd=dest)
    run(str(dest / ".venv" / "Scripts" / "python.exe"), "-c", "import trader.apps.live_app, "
        "trader.gui.live_window", cwd=dest)  # fmt: skip
    print("3. 实盘配置")
    cfg = dest / "config" / "live.yaml"
    prev = previous_config(args.base, dest)
    shutil.copy2(prev or dest / "config" / "live.example.yaml", cfg)
    print(f"  从 {prev or 'config/live.example.yaml（第一次发布）'} 复制到 {cfg}")
    print("4. 启动脚本")
    bat = write_bat(dest, tag, ROOT / "data")
    print(f"  {bat}")
    print(
        f"\n完成。下一步：\n"
        f"  - 打开 {cfg} 检查账户号、风控阈值、上线清单（go_live）；\n"
        f"  - 双击 {bat}。启动检查不通过时窗口里会列出原因（例如策略源码哈希要登记的值）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
