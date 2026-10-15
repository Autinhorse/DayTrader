"""实盘发布检查（DESIGN.md 12.3 第 4 项、12.5）：实盘只运行打了版本标签、没有未提交改动的代码。

实盘版从单独的目录运行（tools/release_live.py 用 git worktree 检出版本标签），
研究目录里改代码、切分支、装依赖都碰不到正在运行的交易进程。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class GitState:
    commit: str = ""
    tags: list[str] = field(default_factory=list)  # 指向当前提交的标签
    dirty: list[str] = field(default_factory=list)  # 未提交的改动（git status --porcelain）
    error: str = ""


def _git(root: Path, *args: str) -> str:
    out = subprocess.run(  # noqa: S603
        ["git", "-C", str(root), *args],  # noqa: S607
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return out.stdout


def git_state(root: Path) -> GitState:
    try:
        commit = _git(root, "rev-parse", "HEAD").strip()
        tags = _git(root, "tag", "--points-at", "HEAD").split()
        dirty = [ln for ln in _git(root, "status", "--porcelain").splitlines() if ln.strip()]
    except (OSError, subprocess.CalledProcessError) as exc:
        return GitState(error=f"无法读取 git 状态：{exc}")
    return GitState(commit, tags, dirty)


def release_problems(state: GitState) -> list[str]:
    """不满足实盘发布条件的原因；空列表表示可以以 live 启动。"""
    if state.error:
        return [state.error]
    out = []
    if state.dirty:
        shown = "；".join(state.dirty[:5]) + ("…" if len(state.dirty) > 5 else "")
        out.append(f"运行目录有未提交的改动（{len(state.dirty)} 处：{shown}）")
    if not state.tags:
        out.append(f"当前提交 {state.commit[:8]} 没有版本标签")
    return out
