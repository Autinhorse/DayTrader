"""源码静态检查：禁止绕过 Clock 读系统时间（DESIGN.md 4.4），以及用户策略的约束（DESIGN.md 7.3）。

包之间的分层规则由 import-linter 检查（pyproject.toml 的 [tool.importlinter]）。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "trader"
USER_STRATEGIES = ROOT / "user" / "strategies"

# 唯一允许读系统时间的文件
CLOCK_FILE = SRC / "core" / "clock.py"

# 用户策略禁止导入的模块（前缀匹配）：券商、数据层、文件与网络读写、系统时间
STRATEGY_FORBIDDEN_IMPORTS = (
    "trader.brokers",
    "trader.data",
    "trader.oms",
    "trader.backtest",
    "trader.research",
    "trader.api",
    "trader.apps",
    "os",
    "io",
    "pathlib",
    "shutil",
    "socket",
    "subprocess",
    "urllib",
    "http",
    "requests",
    "httpx",
    "sqlite3",
    "time",
)
STRATEGY_FORBIDDEN_CALLS = ("open",)


def _system_time_calls(tree: ast.AST) -> list[str]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = node.func.value
            name = owner.id if isinstance(owner, ast.Name) else None
            attr = node.func.attr
            if (name in ("datetime", "date") and attr in ("now", "utcnow", "today")) or (
                name == "time" and attr in ("time", "time_ns")
            ):
                found.append(f"{name}.{attr}() 第 {node.lineno} 行")
        if isinstance(node, ast.ImportFrom) and node.module == "time":
            for alias in node.names:
                if alias.name in ("time", "time_ns"):
                    found.append(f"from time import {alias.name} 第 {node.lineno} 行")
    return found


def _py_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py")) if root.exists() else []


def test_no_system_time_outside_clock():
    problems = []
    for path in _py_files(SRC) + _py_files(USER_STRATEGIES):
        if path == CLOCK_FILE:
            continue
        for hit in _system_time_calls(ast.parse(path.read_text(encoding="utf-8"))):
            problems.append(f"{path.relative_to(ROOT)}: {hit}")
    assert not problems, "只能通过 Clock 取时间：\n" + "\n".join(problems)


def _strategy_violations(source: str) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(source)):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules = [node.module]
        for m in modules:
            if any(m == f or m.startswith(f + ".") for f in STRATEGY_FORBIDDEN_IMPORTS):
                found.append(f"禁止导入 {m}（第 {getattr(node, 'lineno', 0)} 行）")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in STRATEGY_FORBIDDEN_CALLS
        ):
            found.append(f"禁止调用 {node.func.id}()（第 {node.lineno} 行）")
    return found


def test_user_strategies_constraints():
    problems = []
    for path in _py_files(USER_STRATEGIES):
        for hit in _strategy_violations(path.read_text(encoding="utf-8")):
            problems.append(f"{path.relative_to(ROOT)}: {hit}")
    assert not problems, "用户策略违反约束：\n" + "\n".join(problems)


def test_scanners_catch_violations():
    """确认扫描器本身有效，避免目录为空时测试形同虚设。"""
    bad = (
        "import time\n"
        "from datetime import datetime\n"
        "from trader.brokers.ibkr import x\n"
        "t = datetime.now()\n"
        "open('f')\n"
    )
    assert len(_system_time_calls(ast.parse(bad))) == 1
    assert len(_strategy_violations(bad)) == 3
    good = "from trader.strategy import Strategy\nfrom trader.indicators import EMA\n"
    assert _strategy_violations(good) == []
