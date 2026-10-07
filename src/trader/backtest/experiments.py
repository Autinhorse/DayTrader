"""实验记录（DESIGN.md 10.7 第 5 条）：runs/index.sqlite 里每次回测一行，可命名、加备注和标签；
读取单次回测的全部结果；比较多次回测的统计和配置差别。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl

_COLUMNS = {
    "run_id": "TEXT PRIMARY KEY",
    "name": "TEXT",
    "strategy": "TEXT",
    "params": "TEXT",
    "start": "TEXT",
    "end": "TEXT",
    "created": "TEXT",
    "trades": "INTEGER",
    "net_pnl": "REAL",
    "path": "TEXT",
    "git": "TEXT",
    "data": "TEXT",
    "notes": "TEXT DEFAULT ''",
    "tags": "TEXT DEFAULT ''",
    "sweep_id": "TEXT DEFAULT ''",
    "sample": "TEXT DEFAULT ''",  # 参数扫描：in 样本内 / out 样本外
}


def _connect(index: Path) -> sqlite3.Connection:
    index.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(index, timeout=30)
    db.execute(
        "CREATE TABLE IF NOT EXISTS runs ("
        + ", ".join(f'"{k}" {v}' for k, v in _COLUMNS.items())
        + ")"
    )
    have = {r[1] for r in db.execute("PRAGMA table_info(runs)")}
    for k, v in _COLUMNS.items():
        if k not in have:  # 旧版本的索引表，补上新列
            db.execute(f'ALTER TABLE runs ADD COLUMN "{k}" {v.replace("PRIMARY KEY", "")}')
    return db


def register(index: Path, row: dict[str, Any]) -> None:
    with _connect(index) as db:
        cols = list(row)
        db.execute(
            f"INSERT OR REPLACE INTO runs ({', '.join(chr(34) + c + chr(34) for c in cols)}) "
            f"VALUES ({', '.join('?' for _ in cols)})",
            [row[c] for c in cols],
        )


def list_runs(index: Path, strategy: str | None = None, text: str = "") -> list[dict[str, Any]]:
    if not index.exists():
        return []
    with _connect(index) as db:
        db.row_factory = sqlite3.Row
        rows = [
            dict(r) for r in db.execute("SELECT * FROM runs ORDER BY created DESC, run_id DESC")
        ]
    if strategy:
        rows = [r for r in rows if r["strategy"] == strategy]
    if text:
        t = text.lower()
        rows = [
            r
            for r in rows
            if any(
                t in str(r.get(k) or "").lower()
                for k in ("name", "notes", "tags", "params", "run_id")
            )
        ]
    return rows


def update(index: Path, run_id: str, **fields: str) -> None:
    allowed = {k: v for k, v in fields.items() if k in ("name", "notes", "tags")}
    if not allowed:
        return
    with _connect(index) as db:
        db.execute(
            f"UPDATE runs SET {', '.join(f'{k} = ?' for k in allowed)} WHERE run_id = ?",
            [*allowed.values(), run_id],
        )


def delete(index: Path, run_id: str) -> None:
    with _connect(index) as db:
        db.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))


@dataclass
class LoadedRun:
    run_id: str
    path: Path
    config: dict[str, Any]  # 回测配置
    repro: dict[str, Any]  # 可复现信息
    summary: dict[str, Any]
    orders: pl.DataFrame
    fills: pl.DataFrame
    trades: pl.DataFrame
    equity: pl.DataFrame


def load_run(path: Path) -> LoadedRun:
    meta = json.loads((path / "config.json").read_text(encoding="utf-8"))
    return LoadedRun(
        run_id=path.name,
        path=path,
        config=meta["config"],
        repro=meta.get("reproducibility", {}),
        summary=json.loads((path / "summary.json").read_text(encoding="utf-8")),
        orders=pl.read_parquet(path / "orders.parquet"),
        fills=pl.read_parquet(path / "fills.parquet"),
        trades=pl.read_parquet(path / "trades.parquet"),
        equity=pl.read_parquet(path / "equity.parquet"),
    )


# ---------- 比较 ----------

KEY_METRICS: list[tuple[str, str, str]] = [
    # (显示名, summary 中的路径, 格式)
    ("交易笔数", "counts.trades", "int"),
    ("净盈亏", "pnl.net_pnl", "money"),
    ("毛盈亏", "pnl.gross_pnl", "money"),
    ("手续费", "pnl.commission", "money"),
    ("价差成本", "pnl.spread_cost", "money"),
    ("收益率", "pnl.return_pct", "pct100"),
    ("胜率", "performance.win_rate", "pct"),
    ("盈亏比", "performance.payoff_ratio", "num"),
    ("利润因子", "performance.profit_factor", "num"),
    ("期望（每笔）", "performance.expectancy", "money"),
    ("最大回撤", "risk.max_drawdown", "money"),
    ("最大回撤比例", "risk.max_drawdown_pct", "pct"),
    ("日夏普（年化）", "risk.sharpe_daily_annualized", "num"),
    ("平均持仓（分钟）", "holding.avg_holding_min", "num"),
    ("盈亏平衡成本（每股）", "cost_sensitivity.breakeven_extra_cost_per_share", "num"),
    ("路径歧义笔数", "path_ambiguity.trades", "int"),
    ("超出流动性适用范围", "liquidity.out_of_range_trades", "int"),
]


def metric(summary: dict[str, Any], path: str) -> Any:
    cur: Any = summary
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def format_metric(value: Any, fmt: str) -> str:
    if value is None:
        return "-"
    if fmt == "int":
        return f"{int(value):,}"
    if fmt == "money":
        return f"{value:,.2f}"
    if fmt == "pct":
        return f"{value * 100:.1f}%"
    if fmt == "pct100":
        return f"{value:.2f}%"
    return f"{value:,.2f}"


def _flatten(d: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(d, dict):
        out: dict[str, Any] = {}
        for k, v in d.items():
            out |= _flatten(v, f"{prefix}{k}.")
        return out
    return {prefix.rstrip("."): d}


def config_differences(runs: list[LoadedRun]) -> list[tuple[str, list[Any]]]:
    """多次回测在配置（参数、日期、撮合、风控）和数据指纹上的不同之处。"""
    flats = [
        _flatten(r.config)
        | {f"data.{k}": v for k, v in r.repro.get("data_fingerprints", {}).items()}
        | {"git": r.repro.get("git_commit")}
        for r in runs
    ]
    keys = sorted(set().union(*flats))
    return [
        (k, [f.get(k) for f in flats])
        for k in keys
        if len({json.dumps(f.get(k), sort_keys=True, default=str) for f in flats}) > 1
    ]
