"""1 秒 bar 的本地存储（DESIGN.md 5.1）。

路径 data/bars/1s/symbol=AAPL/date=2026-01-23.parquet，每个标的每天一个文件，按 ts_start 升序。
存原始未复权价格。写入先落临时文件再原子替换，中途失败不留半成品。
"""

from __future__ import annotations

import hashlib
import os
from datetime import date
from pathlib import Path

import polars as pl

BAR_SCHEMA: dict[str, pl.DataType] = {
    "ts_start": pl.Int64(),  # UTC 纳秒，区间起点
    "open": pl.Float64(),
    "high": pl.Float64(),
    "low": pl.Float64(),
    "close": pl.Float64(),
    "volume": pl.Float64(),  # Massive 的成交量可能带小数（碎股）
    "vwap": pl.Float64(),
    "trades": pl.Int64(),
}


def empty_bars() -> pl.DataFrame:
    return pl.DataFrame(schema=BAR_SCHEMA)


def fingerprint(df: pl.DataFrame) -> str:
    """内容指纹：只依赖列名和数值，与 parquet 写入库的版本无关。"""
    h = hashlib.sha256()
    for name in BAR_SCHEMA:
        h.update(name.encode())
        h.update(df[name].to_numpy().tobytes())
    return h.hexdigest()


class BarStore:
    def __init__(self, data_dir: Path) -> None:
        self.root = Path(data_dir) / "bars" / "1s"

    def path(self, symbol: str, day: date) -> Path:
        return self.root / f"symbol={symbol}" / f"date={day.isoformat()}.parquet"

    def write_day(self, symbol: str, day: date, df: pl.DataFrame) -> None:
        df = df.select([pl.col(c).cast(t) for c, t in BAR_SCHEMA.items()])
        path = self.path(symbol, day)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        df.write_parquet(tmp, compression="zstd", statistics=True)
        os.replace(tmp, path)

    def read_day(self, symbol: str, day: date) -> pl.DataFrame:
        path = self.path(symbol, day)
        return pl.read_parquet(path) if path.exists() else empty_bars()

    def has_day(self, symbol: str, day: date) -> bool:
        return self.path(symbol, day).exists()

    def delete_day(self, symbol: str, day: date) -> None:
        self.path(symbol, day).unlink(missing_ok=True)

    def symbols(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(p.name.removeprefix("symbol=") for p in self.root.glob("symbol=*"))
