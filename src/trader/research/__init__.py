"""notebook 接口（DESIGN.md 10.6）：界面没有覆盖的自由分析用。

返回值一律是 pandas DataFrame 或简单对象。

    from trader.research import load_bars, compute_indicator, run_backtest, load_run, sweep

    bars = load_bars("AAPL", "1m", "2026-01-05", "2026-01-09")          # 纽约时间索引
    ema = compute_indicator("AAPL", "5m", "ema", "2026-01-05", "2026-01-09", period=20)
    res = run_backtest("ema_cross", {"symbol": "AAPL"}, "2026-01-05", "2026-03-31")
    res.summary; res.trades; res.orders; res.fills; res.equity      # DataFrame
    res.open_in_ui()                                                  # 在研究版界面中打开这次回测
    table = sweep("orb_breakout", {"symbol": "NVDA"}, {"minutes": [15, 30]},
                  "2026-08-01", "2026-09-30", split="2026-09-15")

日期可以写成 "2026-01-05" 或 date；区间两端的交易日都包含。数据目录和项目目录默认取当前目录
（或环境变量 TRADER_HOME / TRADER_DATA_DIR）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import polars as pl

from trader.config import data_dir, project_dir

if TYPE_CHECKING:
    import pandas as pd

__all__ = ["ResearchResult", "compute_indicator", "load_bars", "load_run", "run_backtest", "sweep"]


def _d(x: str | date) -> date:
    return x if isinstance(x, date) else date.fromisoformat(x)


def _services():  # noqa: ANN202
    from trader.core.trading_calendar import TradingCalendar
    from trader.data.catalog import Catalog
    from trader.data.history import HistoryService

    cal = TradingCalendar()
    return HistoryService(data_dir(), cal, Catalog(data_dir() / "catalog.sqlite"))


def _range(history: Any, start: str | date, end: str | date) -> tuple[int, int]:
    days = history.cal.trading_days(_d(start), _d(end))
    if not days:
        raise ValueError(f"{start} ~ {end} 没有交易日")
    return days[0].start, days[-1].end


def _to_pandas(
    df: pl.DataFrame, time_cols: tuple[str, ...] = ("ts_start", "ts_end")
) -> pd.DataFrame:
    """UTC 纳秒列 → 纽约时间的 datetime；第一列时间作为索引。"""
    out = df.to_pandas()
    import pandas as pd

    present = [c for c in time_cols if c in out.columns]
    for c in present:
        out[c] = pd.to_datetime(out[c], unit="ns", utc=True).dt.tz_convert("America/New_York")
    if present:
        out = out.set_index(present[0])
    return out


def load_bars(
    symbol: str,
    timeframe: str,
    start: str | date,
    end: str | date,
    session: str = "rth",
    adjusted: bool = False,
) -> pd.DataFrame:
    """历史 bar：列 open high low close volume vwap trades session，索引为纽约时间的 bar 起点。"""
    h = _services()
    s, e = _range(h, start, end)
    return _to_pandas(h.bars(symbol.upper(), timeframe, s, e, session, adjusted))  # type: ignore[arg-type]


def compute_indicator(
    symbol: str,
    timeframe: str,
    name: str,
    start: str | date,
    end: str | date,
    session: str = "extended",
    **params: Any,
) -> pd.DataFrame:
    """指标历史值（自动预热）。自定义指标会从 user/indicators/ 加载。"""
    from trader.data.indicator_service import IndicatorService
    from trader.indicators.base import load_all

    load_all(project_dir() / "user" / "indicators")
    h = _services()
    s, e = _range(h, start, end)
    df = IndicatorService(h).compute(symbol.upper(), timeframe, name, params, s, e, session)  # type: ignore[arg-type]
    return _to_pandas(df)


@dataclass
class ResearchResult:
    """一次回测的结果。表格都是 pandas DataFrame，时间列为纽约时间。"""

    run_id: str
    path: Path | None
    config: dict[str, Any]
    summary: dict[str, Any]
    trades: pd.DataFrame
    orders: pd.DataFrame
    fills: pd.DataFrame
    equity: pd.DataFrame

    def open_in_ui(self) -> bool:
        """在研究版界面中打开：界面已开着就直接打开；没开就启动它。"""
        if self.path is None:
            raise ValueError("这次回测没有保存（write=False），无法在界面中打开")
        return open_in_ui(self.path)

    def __repr__(self) -> str:
        s = self.summary
        return (
            f"<ResearchResult {self.run_id}: {s['counts']['trades']} 笔交易，"
            f"净盈亏 {s['pnl']['net_pnl']:,.2f}>"
        )


def _from_frames(
    run_id: str,
    path: Path | None,
    config: dict[str, Any],
    summary: dict[str, Any],
    trades: pl.DataFrame,
    orders: pl.DataFrame,
    fills: pl.DataFrame,
    equity: pl.DataFrame,
) -> ResearchResult:
    return ResearchResult(
        run_id=run_id,
        path=path,
        config=config,
        summary=summary,
        trades=_to_pandas(trades, ("entry_time", "exit_time")),
        orders=_to_pandas(orders, ("created_at",)),
        fills=_to_pandas(fills, ("ts",)),
        equity=_to_pandas(equity, ("ts",)),
    )


def run_backtest(
    strategy: str,
    params: dict[str, Any] | None = None,
    start: str | date = "",
    end: str | date = "",
    *,
    name: str = "",
    write: bool = True,
    **config: Any,
) -> ResearchResult:
    """运行一次回测。

    config 里可以写 session、initial_cash、sim={...}、risk={...} 等（见 BacktestConfig）。

    结果保存到 runs/ 并出现在界面的实验列表里（write=False 则不保存）。
    """
    import json

    from trader.backtest.config import BacktestConfig
    from trader.backtest.runner import run_backtest as _run
    from trader.core.clock import WallClock

    cfg = BacktestConfig.model_validate(
        {
            "name": name,
            "strategy": strategy,
            "params": params or {},
            "start": _d(start),
            "end": _d(end),
            **config,
        }
    )
    r = _run(
        cfg,
        data_dir=data_dir(),
        project_dir=project_dir(),
        clock=WallClock(lambda _e: None),
        write=write,
    )
    return _from_frames(
        r.run_id,
        r.out_dir,
        json.loads(cfg.model_dump_json()),
        r.summary,
        r.trades,
        r.orders,
        r.fills,
        r.equity,
    )


def load_run(run: str | Path) -> ResearchResult:
    """按编号（runs/ 下的目录名）或路径读取一次回测的结果。"""
    from trader.backtest.experiments import load_run as _load

    p = Path(run)
    if not p.exists():
        p = project_dir() / "runs" / str(run)
    lr = _load(p)
    return _from_frames(
        lr.run_id, lr.path, lr.config, lr.summary, lr.trades, lr.orders, lr.fills, lr.equity
    )


def sweep(
    strategy: str,
    params: dict[str, Any],
    grid: dict[str, list[Any]],
    start: str | date,
    end: str | date,
    *,
    split: str | date | None = None,
    name: str = "",
    workers: int | None = None,
    **config: Any,
) -> pd.DataFrame:
    """参数扫描（多进程并行）。返回参数对比表；每组参数的单次回测也会保存，可以用 load_run 读取。"""
    from trader.backtest.config import BacktestConfig
    from trader.backtest.sweep import run_sweep
    from trader.core.clock import WallClock
    from trader.core.timeutil import from_ns

    cfg = BacktestConfig.model_validate(
        {
            "name": name,
            "strategy": strategy,
            "params": params,
            "start": _d(start),
            "end": _d(end),
            **config,
        }
    )
    sweep_id = f"{from_ns(WallClock(lambda _e: None).now()):%Y%m%d-%H%M%S}-sweep-{strategy}"
    table = run_sweep(
        cfg,
        {k: [str(v) for v in vals] for k, vals in grid.items()},
        _d(split) if split else None,
        sweep_id=sweep_id,
        data_dir=data_dir(),
        project_dir=project_dir(),
        out_root=project_dir() / "runs",
        workers=workers,
    )
    return table.to_pandas()


# ---------- 在界面中打开 ----------


def open_in_ui(path: Path) -> bool:
    """研究版界面开着：通过本机套接字让它打开 path，返回 True；没开：启动界面并打开，返回 False。"""
    import subprocess
    import sys

    from trader.gui.ipc import IPC_NAME

    try:
        from PySide6.QtNetwork import QLocalSocket

        sock = QLocalSocket()
        sock.connectToServer(IPC_NAME)
        if sock.waitForConnected(500):
            sock.write(str(path).encode("utf-8") + b"\n")
            sock.waitForBytesWritten(1000)
            sock.disconnectFromServer()
            return True
    except ImportError:
        pass
    subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "trader.apps.research_app", "--open", str(path)],
        cwd=project_dir(),
    )
    return False
