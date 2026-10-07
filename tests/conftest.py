"""测试共用：在临时目录里生成合成的 1 秒 bar 数据集。"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from trader.core.timeutil import NS_PER_SEC
from trader.core.trading_calendar import TradingCalendar, TradingDay
from trader.data.catalog import Catalog
from trader.data.history import HistoryService
from trader.data.store import BAR_SCHEMA, BarStore, fingerprint
from trader.data.validate import validate_day

CAL = TradingCalendar()

# 每秒是否有 bar 的过滤函数：(交易日, 距常规开盘的秒数) -> bool
SecondFilter = Callable[[TradingDay, int], bool]


@dataclass
class Dataset:
    root: Path
    cal: TradingCalendar
    catalog: Catalog
    history: HistoryService
    days: list[TradingDay]


def synth_day(
    day: TradingDay, seed: int, keep: SecondFilter | None = None, start_price: float = 100.0
) -> pl.DataFrame:
    """常规时段每秒一根 1 秒 bar，随机游走；keep 返回 False 的秒没有 bar（模拟稀疏成交）。"""
    rnd = random.Random(seed)
    rows = []
    price = start_price
    n = (day.close - day.open) // NS_PER_SEC
    for i in range(n):
        o = price
        c = round(max(1.0, o + rnd.gauss(0, 0.02)), 2)
        if keep is not None and not keep(day, i):
            price = c
            continue
        h = round(max(o, c) + abs(rnd.gauss(0, 0.01)), 2)
        lo = round(min(o, c) - abs(rnd.gauss(0, 0.01)), 2)
        v = float(rnd.randint(200, 3000))
        rows.append(
            (day.open + i * NS_PER_SEC, o, h, lo, c, v, (h + lo + c) / 3, rnd.randint(1, 20))
        )
        price = c
    return pl.DataFrame(rows, schema=BAR_SCHEMA, orient="row")


def make_dataset(
    root: Path,
    days: list[date],
    symbols: tuple[str, ...] = ("AAA",),
    keep: SecondFilter | None = None,
    seed: int = 1,
) -> Dataset:
    store = BarStore(root)
    catalog = Catalog(root / "catalog.sqlite")
    tds = [CAL.trading_day(d) for d in days]
    for k, sym in enumerate(symbols):
        price = 100.0
        for j, td in enumerate(tds):
            df = synth_day(td, seed * 1000 + k * 100 + j, keep, price)
            price = float(df["close"].to_list()[-1])
            store.write_day(sym, td.day, df)
            catalog.record(sym, td.day, validate_day(df, td), fingerprint(df), 0)
    # 项目目录结构（runner 需要）
    (root / "config").mkdir(exist_ok=True)
    (root / "config" / "universe.yaml").write_text(
        "symbols:\n" + "".join(f"  - {s}\n" for s in symbols), encoding="utf-8"
    )
    (root / "user" / "strategies").mkdir(parents=True, exist_ok=True)
    (root / "user" / "indicators").mkdir(parents=True, exist_ok=True)
    return Dataset(root, CAL, catalog, HistoryService(root, CAL, catalog), tds)


@pytest.fixture
def dataset(tmp_path) -> Dataset:
    return make_dataset(tmp_path, [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)])
