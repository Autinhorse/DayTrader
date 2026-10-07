"""聚合一致性核对（DESIGN.md 13）：从 1 秒 bar 聚合的 1 分钟 bar 与 Massive 官方 1 分钟 bar 对比。

两边都是全部时段、原始价格。Massive 的分钟 bar 由它的成交直接生成，我们的由它的 1 秒 bar 合成，
理论上开高低收和成交量应完全一致；不一致的分钟逐条列出。
"""

from __future__ import annotations

from datetime import date

import polars as pl

from trader.core.timeutil import NS_PER_MS, from_ns
from trader.core.trading_calendar import TradingCalendar
from trader.data.history import HistoryService
from trader.data.massive import MassiveClient

COLS = ["open", "high", "low", "close", "volume"]


def massive_minutes(client: MassiveClient, symbol: str, day: date) -> pl.DataFrame:
    d = day.isoformat()
    rows = client._paged(  # noqa: SLF001
        f"{client.base_url}/v2/aggs/ticker/{symbol}/range/1/minute/{d}/{d}",
        {"adjusted": "false", "sort": "asc", "limit": 50000},
    )
    return pl.DataFrame(
        {
            "ts_start": [r["t"] * NS_PER_MS for r in rows],
            "open": [float(r["o"]) for r in rows],
            "high": [float(r["h"]) for r in rows],
            "low": [float(r["l"]) for r in rows],
            "close": [float(r["c"]) for r in rows],
            "volume": [float(r.get("v", 0)) for r in rows],
        },
        schema={"ts_start": pl.Int64, **dict.fromkeys(COLS, pl.Float64)},
    )


def check_day(
    client: MassiveClient, history: HistoryService, cal: TradingCalendar, symbol: str, day: date
) -> dict[str, float | int | list[str]]:
    td = cal.trading_day(day)
    ours = history.day_bars(symbol, "1m", td, "extended").select("ts_start", *COLS)
    theirs = massive_minutes(client, symbol, day)
    j = ours.join(theirs, on="ts_start", how="full", suffix="_m", coalesce=True)
    price_ok = pl.all_horizontal([(pl.col(c) - pl.col(f"{c}_m")).abs() < 1e-6 for c in COLS[:4]])
    vol_ok = (pl.col("volume") - pl.col("volume_m")).abs() <= 1e-6 * pl.col("volume_m").abs() + 1e-6
    both = j.filter(pl.col("open").is_not_null() & pl.col("open_m").is_not_null())
    bad = both.filter(~(price_ok & vol_ok))
    examples = [
        f"{from_ns(r['ts_start']):%H:%M} 我们 O{r['open']} H{r['high']} L{r['low']} C{r['close']} "
        f"V{r['volume']:.0f} | Massive O{r['open_m']} H{r['high_m']} L{r['low_m']} "
        f"C{r['close_m']} V{r['volume_m']:.0f}"
        for r in bad.head(5).iter_rows(named=True)
    ]
    return {
        "ours": ours.height,
        "massive": theirs.height,
        "only_ours": j.filter(pl.col("open_m").is_null()).height,
        "only_massive": j.filter(pl.col("open").is_null()).height,
        "both": both.height,
        "identical": both.height - bad.height,
        "price_diff": both.filter(~price_ok).height,
        "volume_diff": both.filter(~vol_ok).height,
        "examples": examples,
    }
