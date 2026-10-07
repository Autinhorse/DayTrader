"""从 Massive 下载 1 秒 bar 到本地存储（研究版专用）。

增量：只请求区间内尚未处理过的交易日，所以"往前补一段"、"更新到今天"、"新增标的"
都是同一个操作——对指定标的下载指定区间。尚未收盘完毕的当天不下载。
每天的数据经校验后写入并登记到 catalog；校验有错误的日期不写入，下次会重试。

Massive 在收盘后几小时内还会补充迟到的成交（实测收盘后 30 分钟下载的数据，第二天再下载
行数和成交量都会增加）。所以盘后结束不到 FINAL_NS 就下载的数据只算临时数据，下次运行时自动重新下载。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Protocol

import polars as pl

from trader.core.clock import Clock
from trader.core.timeutil import NS_PER_HOUR, NS_PER_MIN, ny_date
from trader.core.trading_calendar import TradingCalendar
from trader.data.catalog import Catalog
from trader.data.corporate_actions import ACTION_SCHEMA, replace_symbol_actions
from trader.data.massive import Cancelled, MassiveClient
from trader.data.store import BarStore, fingerprint
from trader.data.validate import DEFAULT_GAP_THRESHOLD_S, validate_day

# 盘后结束后至少等这么久才下载当天数据
SETTLE_NS = 30 * NS_PER_MIN
# 盘后结束后过了这么久下载的数据才算最终数据；之前下载的下次自动重新下载
FINAL_NS = 8 * NS_PER_HOUR


class BarSource(Protocol):
    """download_symbol 需要的数据源接口；MassiveClient 实现它，测试用假客户端。"""

    stop_event: threading.Event

    def bars_1s(self, symbol: str, day: date) -> pl.DataFrame: ...


Log = Callable[[str], None]
Progress = Callable[[str, int, int], None]  # (标的, 已完成天数, 总天数)


@dataclass(slots=True)
class SymbolResult:
    symbol: str
    planned: int = 0
    stored: int = 0
    empty: int = 0  # 确认没有数据的交易日（例如上市前）
    rows: int = 0
    rejected: list[tuple[date, str]] = field(default_factory=list)
    failed: list[tuple[date, str]] = field(default_factory=list)
    cancelled: bool = False


def complete_days(cal: TradingCalendar, start: date, end: date, now: int) -> list[date]:
    """[start, end] 内已经完整结束的交易日。"""
    end = min(end, ny_date(now))
    if end < start:
        return []
    return [d.day for d in cal.trading_days(start, end) if d.post_close + SETTLE_NS <= now]


def plan_days(
    catalog: Catalog,
    cal: TradingCalendar,
    symbol: str,
    start: date,
    end: date,
    now: int,
    redownload: bool = False,
) -> list[date]:
    days = complete_days(cal, start, end, now)
    if redownload:
        return days
    final = {
        p.day
        for p in catalog.partitions(symbol)
        if p.imported_at >= cal.trading_day(p.day).end + FINAL_NS
    }
    return [d for d in days if d not in final]


def download_symbol(
    client: BarSource,
    store: BarStore,
    catalog: Catalog,
    cal: TradingCalendar,
    clock: Clock,
    symbol: str,
    start: date,
    end: date,
    *,
    redownload: bool = False,
    workers: int = 4,
    gap_threshold_s: int = DEFAULT_GAP_THRESHOLD_S,
    log: Log = print,
    progress: Progress | None = None,
) -> SymbolResult:
    symbol = symbol.upper()
    days = plan_days(catalog, cal, symbol, start, end, clock.now(), redownload)
    res = SymbolResult(symbol, planned=len(days))
    if not days:
        log(f"{symbol}: {start} ~ {end} 没有需要下载的交易日")
        return res
    log(f"{symbol}: 需要下载 {len(days)} 个交易日（{days[0]} ~ {days[-1]}）")

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(client.bars_1s, symbol, d): d for d in days}
        try:
            for fut in as_completed(futures):
                day = futures[fut]
                try:
                    df: pl.DataFrame = fut.result()
                except Cancelled:
                    res.cancelled = True
                    continue
                except Exception as e:  # 单日失败不影响其他日期，下次重试
                    res.failed.append((day, str(e)))
                    log(f"  {symbol} {day} 下载失败：{e}")
                    continue
                _store_day(store, catalog, cal, clock, symbol, day, df, gap_threshold_s, res, log)
                done += 1
                if progress:
                    progress(symbol, done, len(days))
        finally:
            if client.stop_event.is_set():
                res.cancelled = True
                for f in futures:
                    f.cancel()

    log(
        f"{symbol}: 写入 {res.stored} 天（{res.rows:,} 根），无数据 {res.empty} 天，"
        f"校验拒绝 {len(res.rejected)} 天，失败 {len(res.failed)} 天"
        + ("，已停止" if res.cancelled else "")
    )
    return res


def _store_day(
    store: BarStore,
    catalog: Catalog,
    cal: TradingCalendar,
    clock: Clock,
    symbol: str,
    day: date,
    df: pl.DataFrame,
    gap_threshold_s: int,
    res: SymbolResult,
    log: Log,
) -> None:
    check = validate_day(df, cal.trading_day(day), gap_threshold_s)
    if not check.ok:
        res.rejected.append((day, str(check.errors)))
        log(f"  {symbol} {day} 校验未通过，未写入：{check.errors}")
        return
    if df.is_empty():
        store.delete_day(symbol, day)
        catalog.record(symbol, day, check, None, clock.now())
        res.empty += 1
        return
    store.write_day(symbol, day, df)
    catalog.record(symbol, day, check, fingerprint(df), clock.now())
    res.stored += 1
    res.rows += df.height


def refresh_corporate_actions(client: MassiveClient, data_dir: Path, symbol: str) -> int:
    """重新拉取一个标的的全部拆股和分红记录，返回记录数。"""
    rows: list[dict[str, object]] = []
    for s in client.splits(symbol):
        rows.append(
            {
                "symbol": symbol,
                "kind": "split",
                "ex_date": date.fromisoformat(s["execution_date"]),
                "ratio": s["split_to"] / s["split_from"],
                "cash_amount": None,
            }
        )
    for d in client.dividends(symbol):
        if not d.get("ex_dividend_date"):
            continue
        rows.append(
            {
                "symbol": symbol,
                "kind": "dividend",
                "ex_date": date.fromisoformat(d["ex_dividend_date"]),
                "ratio": None,
                "cash_amount": d.get("cash_amount"),
            }
        )
    replace_symbol_actions(data_dir, symbol, pl.DataFrame(rows, schema=ACTION_SCHEMA))
    return len(rows)
