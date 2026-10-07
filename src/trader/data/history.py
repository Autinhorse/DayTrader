"""历史查询服务（DESIGN.md 5.6）：任意标的、任意周期、任意时间范围的 bar。

1 秒 bar 直接读存储；其他周期由 trader.data.aggregate 按天聚合（1 分钟及以上周期的成交量来自
Massive 官方 1 分钟 bar，没下载官方数据的日子退回用 1 秒 bar 加总），并按
(周期, 时段, 标的, 交易日期) 缓存到 data/cache/bars/。缓存文件名带源数据指纹，
源数据重新下载后指纹变化，旧缓存自动失效。

不直接暴露给策略：策略的 ctx.history 另行实现，只返回引擎当前时间之前已可用的数据。
"""

from __future__ import annotations

import os
import uuid
from datetime import date
from pathlib import Path

import polars as pl

from trader.core.aggregation import check_timeframe
from trader.core.trading_calendar import SessionFilter, TradingCalendar, TradingDay
from trader.data.aggregate import OUT_SCHEMA, aggregate_day, label_1s
from trader.data.catalog import Catalog
from trader.data.corporate_actions import adjust_for_splits, load_actions, split_factor
from trader.data.store import BarStore

__all__ = ["HistoryService", "SessionFilter"]


def empty_frame() -> pl.DataFrame:
    return pl.DataFrame(schema=OUT_SCHEMA)


class HistoryService:
    def __init__(
        self, data_dir: Path, cal: TradingCalendar, catalog: Catalog, use_cache: bool = True
    ) -> None:
        self.data_dir = Path(data_dir)
        self.store = BarStore(data_dir)
        self.minute_store = BarStore(data_dir, "1m")
        self.cal = cal
        self.catalog = catalog
        self.use_cache = use_cache
        self.cache_root = self.data_dir / "cache" / "bars"

    def bars(
        self,
        symbol: str,
        timeframe: str,
        start: int,
        end: int,
        session: SessionFilter = "rth",
        adjusted: bool = False,
    ) -> pl.DataFrame:
        """ts_start 在 [start, end)（UTC 纳秒）内的 bar。

        session="rth" 只含常规时段，"extended" 含全部时段。
        日线按交易日，ts_start 是该日第一个纳入时段的开始。
        adjusted=True 时按拆股复权到 end 所在交易日的股本口径；默认返回原始价格。
        """
        check_timeframe(timeframe)
        days = self.cal.days_overlapping(start, end)
        if not days:
            return empty_frame()
        actions = load_actions(self.data_dir) if adjusted else None
        as_of = days[-1].day
        frames = []
        for day in days:
            df = self.day_bars(symbol, timeframe, day, session)
            if df.is_empty():
                continue
            df = df.filter((pl.col("ts_start") >= start) & (pl.col("ts_start") < end))
            if actions is not None:
                df = adjust_for_splits(df, split_factor(actions, symbol, day.day, as_of))
            frames.append(df)
        return pl.concat(frames) if frames else empty_frame()

    def day_bars(
        self, symbol: str, timeframe: str, day: TradingDay, session: SessionFilter
    ) -> pl.DataFrame:
        """一个交易日的全部 bar（原始价格）。"""
        if timeframe == "1s":
            raw = self.store.read_day(symbol, day.day)
            return label_1s(raw, day, session) if not raw.is_empty() else empty_frame()
        fp = self.catalog.fingerprint(symbol, day.day)
        if fp is None:  # 没下载，或这天确认没有数据
            return empty_frame()
        fp_1m = self.catalog.fingerprint(symbol, day.day, kind="bar_1m") or "none"
        path = self._cache_path(symbol, timeframe, day.day, session, f"{fp[:12]}{fp_1m[:8]}")
        if self.use_cache and path.exists():
            try:
                return pl.read_parquet(path)
            except (OSError, pl.exceptions.ComputeError):
                pass  # 另一个进程正在替换这个文件：重新计算
        official = self.minute_store.read_day(symbol, day.day) if fp_1m != "none" else None
        out = aggregate_day(self.store.read_day(symbol, day.day), day, timeframe, session, official)
        if self.use_cache:
            self._write_cache(path, day.day, out)
        return out

    @staticmethod
    def _write_cache(path: Path, day: date, out: pl.DataFrame) -> None:
        """多个回测进程可能同时写同一份缓存：临时文件名各不相同，替换失败也不影响结果。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        for old in path.parent.glob(f"date={day.isoformat()}.*.parquet"):
            if old.name != path.name:  # 只删指纹不同的旧缓存
                old.unlink(missing_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            out.write_parquet(tmp)
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)

    def _cache_path(
        self, symbol: str, timeframe: str, day: date, session: SessionFilter, fp: str
    ) -> Path:
        return (
            self.cache_root
            / timeframe
            / session
            / f"symbol={symbol}"
            / f"date={day.isoformat()}.{fp}.parquet"
        )

    def coverage(self, symbol: str) -> list[date]:
        """有数据的交易日期。"""
        return [p.day for p in self.catalog.partitions(symbol) if p.rows > 0]
