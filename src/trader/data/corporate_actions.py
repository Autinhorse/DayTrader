"""拆股与分红表 data/corporate_actions.parquet（DESIGN.md 5.1），以及按需拆股复权。

bar 一律存原始价格。复权只在查询时进行，只处理拆股：某天的价格除以该天之后（不含该天）
到查询基准日之间所有拆股的累计比例，成交量乘以同样的比例。分红不调整价格。
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import polars as pl

ACTION_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.String(),
    "kind": pl.String(),  # "split" / "dividend"
    "ex_date": pl.Date(),
    "ratio": pl.Float64(),  # 拆股：新股数 / 旧股数，1 拆 10 为 10.0，10 合 1 为 0.1
    "cash_amount": pl.Float64(),  # 分红：每股现金
}


def actions_path(data_dir: Path) -> Path:
    return Path(data_dir) / "corporate_actions.parquet"


def load_actions(data_dir: Path) -> pl.DataFrame:
    path = actions_path(data_dir)
    return pl.read_parquet(path) if path.exists() else pl.DataFrame(schema=ACTION_SCHEMA)


def replace_symbol_actions(data_dir: Path, symbol: str, new: pl.DataFrame) -> None:
    """用 new 替换该标的的全部记录，原子写入。"""
    old = load_actions(data_dir).filter(pl.col("symbol") != symbol)
    out = pl.concat([old, new.select(list(ACTION_SCHEMA)).cast(ACTION_SCHEMA)])  # type: ignore[arg-type]
    out = out.unique().sort("symbol", "ex_date", "kind")
    path = actions_path(data_dir)
    tmp = path.with_name(path.name + ".tmp")
    out.write_parquet(tmp)
    os.replace(tmp, path)


def split_factor(actions: pl.DataFrame, symbol: str, day: date, as_of: date) -> float:
    """day 的原始价格要除以的累计拆股比例（基准日 as_of 的股本口径）。"""
    splits = actions.filter(
        (pl.col("symbol") == symbol)
        & (pl.col("kind") == "split")
        & (pl.col("ex_date") > day)
        & (pl.col("ex_date") <= as_of)
    )
    factor = 1.0
    for r in splits["ratio"].to_list():
        factor *= r
    return factor


def adjust_for_splits(bars: pl.DataFrame, factor: float) -> pl.DataFrame:
    if factor == 1.0:
        return bars
    return bars.with_columns(
        [pl.col(c) / factor for c in ("open", "high", "low", "close", "vwap")]
        + [pl.col("volume") * factor]
    )
