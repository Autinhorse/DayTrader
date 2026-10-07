"""回测运行器（DESIGN.md 10.2、10.3）：run_backtest(config) -> BacktestResult。

命令行 `trader backtest run <config.yaml>`、以后的界面表单和 notebook 接口都调用这一个函数。
结果写入 runs/<run_id>/ 并登记到 runs/index.sqlite。

可复现：记录完整配置、策略源码哈希、git 提交号、所用数据分区的指纹。同样的输入，
业务结果（订单、成交、交易、统计）完全相同；运行编号和生成时间除外。
"""

from __future__ import annotations

import hashlib
import inspect
import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl

from trader.backtest import experiments
from trader.backtest.config import BacktestConfig
from trader.backtest.report import (
    build_trades,
    equity_frame,
    fills_frame,
    orders_frame,
    summarize,
)
from trader.brokers.sim.matcher import SimBroker
from trader.config import load_universe
from trader.core.clock import Clock
from trader.core.timeutil import from_ns
from trader.core.trading_calendar import TradingCalendar
from trader.data.catalog import Catalog
from trader.data.history import HistoryService
from trader.engine.engine import BacktestEngine, EngineConfig, EngineResult
from trader.indicators.base import load_all
from trader.strategy.base import get_strategy, load_user_strategies


@dataclass
class BacktestResult:
    run_id: str
    out_dir: Path | None
    config: BacktestConfig
    summary: dict[str, Any]
    orders: pl.DataFrame
    fills: pl.DataFrame
    trades: pl.DataFrame
    equity: pl.DataFrame
    engine: EngineResult


def _git_commit(root: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        )
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=True
        )
        return out.stdout.strip() + ("+dirty" if dirty.stdout.strip() else "")
    except (OSError, subprocess.CalledProcessError):
        return None


def _data_fingerprints(
    catalog: Catalog, cal: TradingCalendar, symbols: list[str], cfg: BacktestConfig
) -> dict[str, str]:
    """回测区间（加前 30 个交易日预热）所用分区的指纹汇总，每个标的一个哈希。"""
    first = cal.trading_days(cfg.start, cfg.end)
    start = first[0].day if first else cfg.start
    for _ in range(30):
        start = cal.previous_trading_day(start)
    out = {}
    for sym in symbols:
        h = hashlib.sha256()
        for kind in ("bar_1s", "bar_1m"):
            for p in catalog.partitions(sym, kind):
                if start <= p.day <= cfg.end:
                    h.update(f"{kind}{p.day}{p.fingerprint}".encode())
        out[sym] = h.hexdigest()[:16]
    return out


def run_backtest(
    cfg: BacktestConfig,
    *,
    data_dir: Path,
    project_dir: Path,
    clock: Clock,
    out_root: Path | None = None,
    initial_state: dict[str, Any] | None = None,
    write: bool = True,
    progress: Callable[[int, int], None] | None = None,
    index_extra: dict[str, str] | None = None,
) -> BacktestResult:
    """运行一次回测。

    progress(已完成交易日数, 总交易日数)；
    index_extra 写入实验索引（notes、tags、sweep_id、sample）。
    """
    errors = load_all(project_dir / "user" / "indicators")
    errors |= load_user_strategies(project_dir / "user" / "strategies")
    if errors:
        raise RuntimeError(
            "加载用户指标或策略失败：" + "；".join(f"{k}: {v}" for k, v in errors.items())
        )
    strategy_cls = get_strategy(cfg.strategy)
    params = strategy_cls.Params(**cfg.params)

    cal = TradingCalendar()
    catalog = Catalog(data_dir / "catalog.sqlite")
    try:
        history = HistoryService(data_dir, cal, catalog)
        symbols = cfg.symbols or load_universe(project_dir / "config" / "universe.yaml").names
        sim = cfg.sim.model_copy()
        universe = {
            s.symbol: s for s in load_universe(project_dir / "config" / "universe.yaml").symbols
        }
        spreads = dict(sim.half_spread_bps)
        for sym, sc in universe.items():
            if sc.half_spread is not None and sym not in spreads:
                spreads[sym] = sc.half_spread
        sim.half_spread_bps = spreads
        engine = BacktestEngine(
            history,
            SimBroker(sim),
            cfg.risk,
            EngineConfig(
                strategy_id=cfg.strategy,
                session=cfg.session,
                trade_extended=cfg.trade_extended,
                hold_post=cfg.hold_post,
                initial_cash=Decimal(str(cfg.initial_cash)),
                decision_delay_ms=sim.decision_delay_ms,
                bar_grace_ms=sim.bar_grace_ms,
                whitelist=frozenset(symbols),
                initial_state=initial_state or {},
            ),
        )
        days = cal.trading_days(cfg.start, cfg.end)
        if not days:
            raise ValueError(f"{cfg.start} ~ {cfg.end} 没有交易日")
        res = engine.run(
            strategy_cls,
            params,
            days[0].start,
            days[-1].end,
            progress=(lambda k: progress(k, len(days))) if progress else None,
        )

        orders = orders_frame(res.orders)
        fills = fills_frame(res)
        trades = build_trades(res, history, cfg.session)
        equity = equity_frame(res)
        traded = sorted(set(trades["symbol"].to_list())) if trades.height else []
        gap_days = {
            sym: sum(1 for p in catalog.partitions(sym) if cfg.start <= p.day <= cfg.end and p.gaps)
            for sym in sorted({o.intent.symbol for o in res.orders} | set(traded))
        }
        summary = summarize(
            res,
            trades,
            orders,
            equity,
            cal,
            cfg.initial_cash,
            sim.cost_scenarios,
            {
                "days_with_rth_gaps": gap_days,
                "note": "常规时段内连续 60 秒以上没有成交的日子数；这些日子的成交假设更粗糙",
            },
        )
        repro = {
            "strategy_source_sha256": hashlib.sha256(
                inspect.getsource(inspect.getmodule(strategy_cls)).encode()  # type: ignore[arg-type]
            ).hexdigest()[:16],
            "git_commit": _git_commit(project_dir),
            "data_fingerprints": _data_fingerprints(
                catalog, cal, sorted(set(symbols) & (set(traded) or set(symbols))), cfg
            ),
        }
        created = from_ns(clock.now())
        run_id = f"{created:%Y%m%d-%H%M%S}-{cfg.strategy}"
        result = BacktestResult(run_id, None, cfg, summary, orders, fills, trades, equity, res)
        if write:
            out_root = out_root or project_dir / "runs"
            result.out_dir = _write(result, out_root, repro, created.isoformat(), index_extra or {})
        return result
    finally:
        catalog.close()


def _write(
    r: BacktestResult, out_root: Path, repro: dict[str, Any], created: str, extra: dict[str, str]
) -> Path:
    out_root.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:  # 并行运行时可能同名，靠 mkdir 的原子性避免冲突
        out = out_root / (r.run_id if n == 1 else f"{r.run_id}-{n}")
        try:
            out.mkdir()
            break
        except FileExistsError:
            n += 1
    r.run_id = out.name
    cfg_json = json.loads(r.config.model_dump_json())
    (out / "config.json").write_text(
        json.dumps(
            {"config": cfg_json, "reproducibility": repro, "created": created},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    r.orders.write_parquet(out / "orders.parquet")
    r.fills.write_parquet(out / "fills.parquet")
    r.trades.write_parquet(out / "trades.parquet")
    r.equity.write_parquet(out / "equity.parquet")
    pl.DataFrame(r.engine.marks, infer_schema_length=None).write_parquet(out / "marks.parquet")
    with (out / "log.jsonl").open("w", encoding="utf-8") as f:
        for row in r.engine.logs:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    (out / "summary.json").write_text(
        json.dumps(r.summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    experiments.register(
        out_root / "index.sqlite",
        {
            "run_id": out.name,
            "name": r.config.name,
            "strategy": r.config.strategy,
            "params": json.dumps(r.config.params, ensure_ascii=False, sort_keys=True),
            "start": r.config.start.isoformat(),
            "end": r.config.end.isoformat(),
            "created": created,
            "trades": r.summary["counts"]["trades"],
            "net_pnl": r.summary["pnl"]["net_pnl"],
            "path": str(out),
            "git": repro.get("git_commit"),
            "data": json.dumps(repro.get("data_fingerprints")),
            **extra,
        },
    )
    return out


def business_view(r: BacktestResult) -> dict[str, Any]:
    """规范化的业务结果，用于确定性比较（不含运行编号、生成时间）。"""
    return {
        "orders": r.orders.drop("client_order_id", "parent_id").to_dicts(),
        "fills": r.fills.drop("exec_id", "client_order_id").to_dicts(),
        "trades": r.trades.to_dicts(),
        "summary": r.summary,
    }
