"""参数扫描（DESIGN.md 10.5）：给参数填多个取值，按网格展开，多进程并行；
可把日期区间切成样本内和样本外两段，同一组参数两段各跑一次，用来识别过拟合。

每次回测都是独立的结果（在实验列表里可以单独打开），另外输出一张参数对比表
runs/sweeps/<sweep_id>.parquet。
"""

from __future__ import annotations

import itertools
import os
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from trader.backtest.config import BacktestConfig
from trader.backtest.experiments import metric


def expand_grid(grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """{"fast": [5, 9], "slow": [21, 34]} → 4 组参数（按键名顺序，确定性）。"""
    keys = sorted(grid)
    return [
        dict(zip(keys, combo, strict=True)) for combo in itertools.product(*(grid[k] for k in keys))
    ]


def parse_values(text: str) -> list[str]:
    """表单里一个参数格子的文字：逗号分隔多个取值，或 起点:终点:步长。"""
    text = text.strip()
    if ":" in text and "," not in text:
        parts = text.split(":")
        if len(parts) == 3:
            a, b, step = (float(x) for x in parts)
            if step <= 0 or b < a:
                raise ValueError(f"范围写法不对：{text}")
            vals, x = [], a
            while x <= b + 1e-9:
                vals.append(x)
                x += step
            as_int = all(float(v).is_integer() for v in (a, b, step))
            return [str(int(v)) if as_int else f"{v:g}" for v in vals]
    return [v.strip() for v in text.split(",") if v.strip()]


def plan(
    base: BacktestConfig, grid: dict[str, list[Any]], split: date | None
) -> list[tuple[dict[str, Any], str, BacktestConfig]]:
    """展开成 (参数组合, 样本标记, 配置) 列表。split 为样本外的第一天。"""
    out = []
    for combo in expand_grid(grid):
        params = {**base.params, **combo}
        label = " ".join(f"{k}={v}" for k, v in combo.items())
        name = f"{base.name or base.strategy} [{label}]"
        if split is None:
            out.append((combo, "", base.model_copy(update={"params": params, "name": name})))
            continue
        if not (base.start < split <= base.end):
            raise ValueError("样本外起始日必须在回测区间之内")
        out.append(
            (
                combo,
                "in",
                base.model_copy(
                    update={
                        "params": params,
                        "name": f"{name} 样本内",
                        "end": split - timedelta(days=1),
                    }
                ),
            )
        )
        out.append(
            (
                combo,
                "out",
                base.model_copy(
                    update={"params": params, "name": f"{name} 样本外", "start": split}
                ),
            )
        )
    return out


def _run_one(
    args: tuple[dict[str, Any], dict[str, Any], str, str, str],
) -> tuple[str, str, dict[str, Any]]:
    cfg_json, extra, data_dir, project_dir, out_root = args
    from trader.backtest.runner import run_backtest  # 子进程里导入
    from trader.core.clock import WallClock

    cfg = BacktestConfig.model_validate(cfg_json)
    r = run_backtest(
        cfg,
        data_dir=Path(data_dir),
        project_dir=Path(project_dir),
        clock=WallClock(lambda _e: None),
        out_root=Path(out_root),
        index_extra=extra,
    )
    return r.run_id, str(r.out_dir), r.summary


SUMMARY_COLUMNS = [
    ("trades", "counts.trades"),
    ("net_pnl", "pnl.net_pnl"),
    ("win_rate", "performance.win_rate"),
    ("profit_factor", "performance.profit_factor"),
    ("max_drawdown", "risk.max_drawdown"),
    ("sharpe", "risk.sharpe_daily_annualized"),
]


def run_sweep(
    base: BacktestConfig,
    grid: dict[str, list[Any]],
    split: date | None,
    *,
    sweep_id: str,
    data_dir: Path,
    project_dir: Path,
    out_root: Path,
    notes: str = "",
    tags: str = "",
    workers: int | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> pl.DataFrame:
    jobs = plan(base, grid, split)
    workers = workers or max(1, min(len(jobs), (os.cpu_count() or 2) - 1))
    rows: dict[str, dict[str, Any]] = {}
    keys = [" ".join(f"{k}={v}" for k, v in combo.items()) for combo, _, _ in jobs]
    for (combo, _, _), key in zip(jobs, keys, strict=True):
        rows.setdefault(key, {**{f"param_{k}": str(v) for k, v in combo.items()}})
    args = [
        (
            json_cfg(cfg),
            {"notes": notes, "tags": tags, "sweep_id": sweep_id, "sample": sample},
            str(data_dir),
            str(project_dir),
            str(out_root),
        )
        for _, sample, cfg in jobs
    ]
    done = 0
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futs = {
            pool.submit(_run_one, a): (k, jobs[i][1])
            for i, (a, k) in enumerate(zip(args, keys, strict=True))
        }
        for fut in as_completed(futs):
            key, sample = futs[fut]
            run_id, _path, summary = fut.result()
            prefix = {"in": "in_", "out": "out_", "": ""}[sample]
            row = rows[key]
            row[f"{prefix}run_id"] = run_id
            for col, path in SUMMARY_COLUMNS:
                v = metric(summary, path)
                row[f"{prefix}{col}"] = float(v) if v is not None else None
            done += 1
            if progress:
                progress(done, len(jobs), run_id)
    table = pl.DataFrame(list(rows.values()), infer_schema_length=None)
    sort_col = "in_net_pnl" if split is not None else "net_pnl"
    if sort_col in table.columns:
        table = table.sort(sort_col, descending=True, nulls_last=True)
    (out_root / "sweeps").mkdir(parents=True, exist_ok=True)
    table.write_parquet(out_root / "sweeps" / f"{sweep_id}.parquet")
    return table


def json_cfg(cfg: BacktestConfig) -> dict[str, Any]:
    import json

    return json.loads(cfg.model_dump_json())
