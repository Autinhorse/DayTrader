"""回测子进程（DESIGN.md 10.2：回测在独立进程运行，不占用界面）。

    python -m trader.backtest.worker job.json

job.json：{"config": {...回测配置...}, "grid": {"fast": ["5", "9"]} 或 {},
           "split": "2026-09-15" 或 null, "notes": "", "tags": "",
           "data_dir": "...", "project_dir": "...", "out_root": "..."}
标准输出每行一个 JSON：
    {"type": "progress", "done": 3, "total": 21, "label": "..."}
    {"type": "done", "run_ids": [...], "sweep_id": "..." 或 null}
    {"type": "error", "message": "..."}
"""

from __future__ import annotations

import json
import sys
import traceback
from datetime import date
from pathlib import Path
from typing import Any


def emit(**kw: Any) -> None:
    print(json.dumps(kw, ensure_ascii=False, default=str), flush=True)


def main(argv: list[str]) -> int:
    from trader.backtest.config import BacktestConfig
    from trader.backtest.runner import run_backtest
    from trader.backtest.sweep import run_sweep
    from trader.core.clock import WallClock
    from trader.core.timeutil import from_ns

    job = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    cfg = BacktestConfig.model_validate(job["config"])
    data_dir, project_dir = Path(job["data_dir"]), Path(job["project_dir"])
    out_root = Path(job.get("out_root") or project_dir / "runs")
    grid = {k: v for k, v in (job.get("grid") or {}).items() if v}
    split = date.fromisoformat(job["split"]) if job.get("split") else None
    clock = WallClock(lambda _e: None)
    if not grid and split is None:
        r = run_backtest(
            cfg,
            data_dir=data_dir,
            project_dir=project_dir,
            clock=clock,
            out_root=out_root,
            progress=lambda k, n: emit(
                type="progress", done=k, total=n, label=f"第 {k + 1}/{n} 个交易日"
            ),
            index_extra={"notes": job.get("notes", ""), "tags": job.get("tags", "")},
        )
        emit(type="done", run_ids=[r.run_id], sweep_id=None)
        return 0
    sweep_id = f"{from_ns(clock.now()):%Y%m%d-%H%M%S}-sweep-{cfg.strategy}"
    table = run_sweep(
        cfg,
        grid,  # 只切样本内外、不扫描参数时为空：展开成一组
        split,
        sweep_id=sweep_id,
        data_dir=data_dir,
        project_dir=project_dir,
        out_root=out_root,
        notes=job.get("notes", ""),
        tags=job.get("tags", ""),
        progress=lambda k, n, rid: emit(type="progress", done=k, total=n, label=rid),
    )
    ids = [v for c in table.columns if c.endswith("run_id") for v in table[c].to_list() if v]
    emit(type="done", run_ids=ids, sweep_id=sweep_id)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as e:  # 子进程的任何错误都报告给界面
        emit(type="error", message=f"{type(e).__name__}: {e}", trace=traceback.format_exc())
        sys.exit(1)
