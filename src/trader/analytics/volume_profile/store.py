"""每日细网格缓存（规范 5.1，决策 0017）。

data/profiles/<构建参数>/symbol=X/date=Y.<源数据指纹>.parquet，列 segment、price_cents、volume；
当天的统计（常规时段开高低收、清洗计数、各分段输入成交量）放在文件元数据里。价格为当天实际价格。
源数据重新下载后指纹变化，旧文件自动失效；只计算缺的日子。
"""

from __future__ import annotations

import json
import math
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

import numpy as np
import polars as pl

from trader.analytics.volume_profile.day import SEG_CODE, SEGMENTS, DayBars, grids, to_source
from trader.analytics.volume_profile.params import ProfileParams
from trader.core import volume_profile as vp
from trader.core.trading_calendar import TradingDay
from trader.data.corporate_actions import load_actions, split_factor
from trader.data.history import HistoryService


@dataclass(frozen=True, slots=True)
class DayStats:
    day: date
    source_bar: str  # "1s" / "1m"
    rth_open: float  # 清洗后常规时段（9:30–16:00）的开高低收，当天实际价格
    rth_high: float
    rth_low: float
    rth_close: float
    early_close: bool
    filter_limit: float  # 离群阈值（价格）；nan 表示之前数据不足 5 天，未修正
    fixed_prints: int  # 被修正或删除的 1 秒 bar 数
    window_bars: int  # 落在坏数据时段里被删掉的 bar 数
    volume_in: dict[str, float]  # 各分段的输入成交量（清洗后）
    volume_small: dict[str, float]  # 其中因 min_bar_volume 未参与的

    @property
    def rth_range(self) -> float:
        return self.rth_high - self.rth_low


@dataclass(frozen=True, slots=True)
class DayGrid:
    stats: DayStats
    grids: dict[str, vp.FineProfile]

    def select(self, segments: tuple[str, ...]) -> vp.FineProfile:
        return vp.merge(self.grids[s] for s in segments if s in self.grids)


class ProfileStore:
    def __init__(self, history: HistoryService, params: ProfileParams) -> None:
        self.history = history
        self.cal = history.cal
        self.p = params
        self.root = history.data_dir / "profiles" / params.build_key()
        self.actions = load_actions(history.data_dir)
        self._days: dict[str, dict[date, tuple[str, str]]] = {}  # symbol → {日期: (类型, 指纹)}
        self._grids: dict[tuple[str, date], DayGrid] = {}
        self._raw_range: dict[tuple[str, date], float] = {}

    # ---------- 有数据的日子 ----------

    def _available(self, symbol: str) -> dict[date, tuple[str, str]]:
        if symbol not in self._days:
            out: dict[date, tuple[str, str]] = {}
            cat = self.history.catalog
            for part in cat.partitions(symbol, "bar_1m"):
                if part.rows > 0 and part.fingerprint:
                    out[part.day] = ("1m", part.fingerprint)
            for part in cat.partitions(symbol):
                if part.rows > 0 and part.fingerprint:
                    out[part.day] = ("1s", part.fingerprint)
            self._days[symbol] = out
        return self._days[symbol]

    def has_data(self, symbol: str, day: date) -> bool:
        return day in self._available(symbol)

    def days_before(self, symbol: str, day: date, n: int) -> list[TradingDay]:
        """day 之前（不含）最近 n 个有数据的交易日，按时间顺序。"""
        avail = self._available(symbol)
        out: list[TradingDay] = []
        d = day
        first = min(avail) if avail else day
        while len(out) < n and d > first:
            d = self.cal.previous_trading_day(d)
            if d in avail:
                out.append(self.cal.trading_day(d))
        return out[::-1]

    def days_between(self, symbol: str, start: date, end: date) -> list[TradingDay]:
        avail = self._available(symbol)
        return [td for td in self.cal.trading_days(start, end) if td.day in avail]

    def factor(self, symbol: str, day: date, basis: date) -> float:
        """day 的实际价格换算到 basis 当天口径要除以的拆股比例（basis 可以早于 day）。"""
        if basis >= day:
            return split_factor(self.actions, symbol, day, basis)
        return 1.0 / split_factor(self.actions, symbol, basis, day)

    # ---------- 清洗阈值与 ADR ----------

    def raw_range(self, symbol: str, td: TradingDay) -> float:
        key = (symbol, td.day)
        if key not in self._raw_range:
            m1 = self.history.day_bars(symbol, "1m", td, "rth")
            self._raw_range[key] = (
                float(m1["high"].max() - m1["low"].min())  # type: ignore[operator]
                if not m1.is_empty()
                else math.nan
            )
        return self._raw_range[key]

    def filter_limit(self, symbol: str, td: TradingDay) -> float:
        """离群阈值：print_filter_adr × 之前（原始）振幅的中位数，与日内波动报表相同；
        跨拆股先换算到当天口径。之前不足 5 天时不修正（不用当天的数据，保证时点正确）。"""
        n = self.p.adr_days
        prior = [d for d in self.days_before(symbol, td.day, n + 5) if not d.early_close][-n:]
        rng = [self.raw_range(symbol, d) / self.factor(symbol, d.day, td.day) for d in prior]
        rng = [r for r in rng if math.isfinite(r)]
        if len(rng) < 5:
            return math.nan
        return self.p.print_filter_adr * float(np.median(rng))

    def adr_through(self, symbol: str, day: date) -> float:
        """截至 day 收盘（含 day）的 ADR，day 当天的价格口径：最近 adr_days 个常规交易日
        （不含提前收盘日）清洗后振幅的中位数；不足 adr_min_days 天为 nan。"""
        n = self.p.adr_days
        tds = self.days_before(symbol, day, n + 5)
        if self.has_data(symbol, day):
            tds.append(self.cal.trading_day(day))
        tds = [d for d in tds if not d.early_close][-n:]
        rng = []
        for d in tds:
            g = self.day(symbol, d)
            if g is not None and math.isfinite(g.stats.rth_range):
                rng.append(g.stats.rth_range / self.factor(symbol, d.day, day))
        if len(rng) < self.p.adr_min_days:
            return math.nan
        return float(np.median(rng))

    # ---------- 读取与构建 ----------

    def load_bars(self, symbol: str, td: TradingDay) -> DayBars:
        """当天的 bar（清洗前，带清洗阈值），developing / premarket 用。"""
        h = self.history
        fine = h.store.read_day(symbol, td.day)
        minute = h.minute_store.read_day(symbol, td.day) if fine.is_empty() else None
        windows = [w.ns_range() for w in self.p.bad_windows or () if w.applies(symbol, td.day)]
        return DayBars.load(
            td, fine, minute, self.filter_limit(symbol, td), windows, self.p.clean_half
        )

    def _path(self, symbol: str, day: date, fp: str) -> Path:
        return self.root / f"symbol={symbol}" / f"date={day.isoformat()}.{fp[:12]}.parquet"

    def day(self, symbol: str, td: TradingDay) -> DayGrid | None:
        """一天的细网格（读缓存，没有就计算并写入）。没有数据时为 None。"""
        key = (symbol, td.day)
        if key in self._grids:
            return self._grids[key]
        info = self._available(symbol).get(td.day)
        if info is None:
            return None
        path = self._path(symbol, td.day, info[1])
        g = _read(path) if path.exists() else None
        if g is None:
            g = self.build(symbol, td)
            _write(path, g)
        self._grids[key] = g
        return g

    def build(self, symbol: str, td: TradingDay) -> DayGrid:
        db = self.load_bars(symbol, td)
        b, seg, fixed = db.full()
        rth = (seg == SEG_CODE["rth"]) | (seg == SEG_CODE["open_cross"])
        rth |= (seg == SEG_CODE["close_cross"]) & (b.ts < td.close)
        if rth.any():
            o, c = float(b.open[rth][0]), float(b.close[rth][-1])
            hi, lo = float(b.high[rth].max()), float(b.low[rth].min())
        else:
            o = c = hi = lo = math.nan
        b2, seg2 = to_source(b, seg, td, self.p.source, db.source_bar)
        gs, vin, small = grids(b2, seg2, self.p)
        stats = DayStats(
            td.day, db.source_bar, o, hi, lo, c, td.early_close, db.limit, fixed,
            db.window_bars, vin, small,
        )  # fmt: skip
        return DayGrid(stats, gs)


def _write(path: Path, g: DayGrid) -> None:
    rows_seg: list[str] = []
    rows_px: list[np.ndarray] = []
    rows_v: list[np.ndarray] = []
    for name, fp in g.grids.items():
        nz = np.flatnonzero(fp.vol > 0)
        rows_seg += [name] * len(nz)
        rows_px.append(fp.base + nz)
        rows_v.append(fp.vol[nz])
    df = pl.DataFrame(
        {
            "segment": pl.Series(rows_seg, dtype=pl.String),
            "price_cents": pl.Series(np.concatenate(rows_px) if rows_px else [], dtype=pl.Int64),
            "volume": pl.Series(np.concatenate(rows_v) if rows_v else [], dtype=pl.Float64),
        }
    )
    meta = asdict(g.stats)
    meta["day"] = g.stats.day.isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    for old in path.parent.glob(f"{path.name.split('.')[0]}.*.parquet"):
        if old.name != path.name:
            old.unlink(missing_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        df.write_parquet(tmp, metadata={"stats": json.dumps(meta, allow_nan=True)})
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)


def _read(path: Path) -> DayGrid | None:
    try:
        meta = json.loads(pl.read_parquet_metadata(path)["stats"])
        df = pl.read_parquet(path)
    except (OSError, KeyError, ValueError, pl.exceptions.ComputeError):
        return None  # 另一个进程正在替换，或旧格式：重新计算
    meta["day"] = date.fromisoformat(meta["day"])
    stats = DayStats(**meta)
    gs: dict[str, vp.FineProfile] = {}
    for name in SEGMENTS:
        part = df.filter(pl.col("segment") == name)
        if part.is_empty():
            continue
        px = part["price_cents"].to_numpy()
        base = int(px.min())
        vol = np.zeros(int(px.max()) - base + 1)
        vol[px - base] = part["volume"].to_numpy()
        gs[name] = vp.FineProfile(base, vol)
    return DayGrid(stats, gs)
