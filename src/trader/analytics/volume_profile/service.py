"""各种时间窗口的 Profile、关键价位表、developing 序列、naked POC（规范第 4、5 节）。

时点规则（规范 4.1）：在交易日 T、时刻 t——
    daily / composite / decay / postmarket / anchored（不含当天部分）只用 T−1 及之前的完整交易日；
    developing、premarket、anchored 的当天部分只用 t 之前已收盘的 bar，清洗也只用 t 之前的数据。
价格口径：T 当天的实际价格（之前的日子按拆股换算）。ADR、格子大小用 T 开盘时已知的值（截至 T−1）。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any

import numpy as np
import pandas as pd

from trader.analytics.frames import rows as select_rows
from trader.analytics.volume_profile.day import (
    SEG_CODE,
    DayBars,
    grids,
    session_segments,
    to_source,
)
from trader.analytics.volume_profile.params import ProfileParams
from trader.analytics.volume_profile.store import ProfileStore
from trader.core import volume_profile as vp
from trader.core.timeutil import NY, from_ns, ny_to_ns, timeframe_ns
from trader.core.trading_calendar import TradingDay
from trader.data.history import HistoryService

MIN_NS = 60 * 10**9


@dataclass(frozen=True, slots=True)
class WindowProfile:
    """一个时间窗口的 Profile（T 当天的价格口径）。"""

    symbol: str
    day: date  # T
    profile_type: str
    params: dict[str, Any]
    fine: vp.FineProfile
    as_of: int  # 所用数据的截止时刻（UTC 纳秒）
    days: tuple[date, ...]  # 用到的交易日
    source_bar: str


@dataclass(frozen=True, slots=True)
class RangeProfile:
    """get_profile 的结果：bins 每行一个格子（price_low、price_high、price_mid、volume）。"""

    bins: pd.DataFrame
    levels: vp.Levels | None
    fine: vp.FineProfile
    bin_size: float
    adr: float
    days: tuple[date, ...] = field(default_factory=tuple)


def as_date(x: str | date) -> date:
    return x if isinstance(x, date) else date.fromisoformat(x)


class VolumeProfile:
    """研究用的 Volume Profile 服务。history 默认用项目的数据目录。"""

    def __init__(self, params: ProfileParams | None = None, history: HistoryService | None = None):
        p = params or ProfileParams()
        if history is None:
            history = _default_history()
        if p.bad_windows is None:
            from trader.analytics.cleaning import load_bad_windows
            from trader.config import project_dir

            p = p.with_(
                bad_windows=load_bad_windows(project_dir() / "config" / "bad_data_windows.yaml")
            )
        self.p = p
        self.history = history
        self.cal = history.cal
        self.store = ProfileStore(history, p)
        self._bars_cache: dict[tuple[str, date], DayBars] = {}

    # ---------- 基础量 ----------

    def td(self, day: str | date) -> TradingDay:
        return self.cal.trading_day(as_date(day))

    def prev_day(self, symbol: str, day: date) -> TradingDay | None:
        prev = self.store.days_before(symbol, day, 1)
        return prev[0] if prev else None

    def adr(self, symbol: str, day: str | date) -> float:
        """T 开盘时已知的 ADR（截至 T−1），T 的价格口径。"""
        t = as_date(day)
        prev = self.prev_day(symbol, t)
        if prev is None:
            return math.nan
        return self.store.adr_through(symbol, prev.day) / self.store.factor(symbol, prev.day, t)

    def prev_close(self, symbol: str, day: str | date) -> float:
        t = as_date(day)
        prev = self.prev_day(symbol, t)
        g = self.store.day(symbol, prev) if prev else None
        if prev is None or g is None:
            return math.nan
        return g.stats.rth_close / self.store.factor(symbol, prev.day, t)

    def bin_size(self, adr: float, price: float) -> int:
        """格子大小（分）。ADR 不可用时退回 pct 模式。"""
        p = self.p
        mode = p.bin_mode
        if mode == "adr" and not math.isfinite(adr):
            mode = "pct"
        return vp.bin_cents(
            mode, adr=adr, price=price, adr_frac=p.bin_adr_frac, pct=p.bin_pct, fixed=p.bin_fixed
        )

    def _day_grid(
        self, symbol: str, td: TradingDay, basis: date, session: str | None = None
    ) -> vp.FineProfile:
        g = self.store.day(symbol, td)
        if g is None:
            return vp.FineProfile.empty()
        fp = g.select(session_segments(session or self.p.session, self.p))
        return fp.split_adjusted(self.store.factor(symbol, td.day, basis))

    def _bars(self, symbol: str, td: TradingDay) -> DayBars:
        key = (symbol, td.day)
        if key not in self._bars_cache:
            if len(self._bars_cache) > 8:
                self._bars_cache.pop(next(iter(self._bars_cache)))
            self._bars_cache[key] = self.store.load_bars(symbol, td)
        return self._bars_cache[key]

    def _prefix_grid(
        self, symbol: str, td: TradingDay, t: int, segments: tuple[str, ...], since: int = 0
    ) -> vp.FineProfile:
        """当天 t 之前已收盘、ts ≥ since 的 bar（只用 t 之前的数据清洗）在给定分段上的细网格。"""
        db = self._bars(symbol, td)
        b, seg = db.prefix(t)
        b, seg = to_source(b, seg, td, self.p.source, db.source_bar)
        bar_ns = db.bar_ns if self.p.source == "1s" else timeframe_ns(self.p.source)
        bar_ns = max(bar_ns, db.bar_ns)
        keep = (b.ts + bar_ns <= t) & (b.ts >= since)
        seg = np.where(keep, seg, -1)
        gs, _, _ = grids(b, seg, self.p, segments)
        return vp.merge(gs.values())

    # ---------- 时间窗口（规范第 4 节）----------

    def window(self, symbol: str, day: str | date, profile_type: str, **kw: Any) -> WindowProfile:
        """交易日 T 的一个时间窗口。

        daily(session=)：T−1；composite(n=)：T 之前 n 个交易日；
        decay(half_life=)：之前的交易日按半衰期加权；postmarket：T−1 盘后；
        anchored(anchor="auto" | 日期 | "YYYY-MM-DD HH:MM", until=)；
        premarket(until=)；developing(until=)。
        until 是纽约时间（"10:30"、time 或 UTC 纳秒），缺省为开盘（developing 为收盘）。
        """
        symbol = symbol.upper()
        t = as_date(day)
        td = self.cal.trading_day(t)
        p = self.p
        src = "1s"
        used: list[date] = []

        def add_days(tds: list[TradingDay], weights: list[float] | None = None,
                     session: str | None = None) -> vp.FineProfile:  # fmt: skip
            nonlocal src
            out = []
            for d in tds:
                g = self.store.day(symbol, d)
                if g is not None and g.stats.source_bar != "1s":
                    src = g.stats.source_bar
                out.append(self._day_grid(symbol, d, t, session))
                used.append(d.day)
            return vp.merge(out, weights)

        if profile_type == "daily":
            session = kw.get("session", p.session)
            fp = add_days(self.store.days_before(symbol, t, 1), session=session)
            as_of = td.start
        elif profile_type == "composite":
            n = int(kw["n"])
            fp = add_days(self.store.days_before(symbol, t, n))
            as_of = td.start
        elif profile_type == "decay":
            h = float(kw["half_life"])
            tds = self.store.days_before(symbol, t, max(1, math.ceil(h * p.decay_max_half_lives)))
            ages = range(len(tds), 0, -1)
            fp = add_days(tds, [0.5 ** (a / h) for a in ages])
            as_of = td.start
        elif profile_type == "postmarket":
            fp = add_days(self.store.days_before(symbol, t, 1), session="post")
            as_of = td.start
        elif profile_type in ("premarket", "developing"):
            default = td.open if profile_type == "premarket" else td.close
            until = self._until(td, kw.get("until"), default)
            if profile_type == "premarket":
                until = min(until, td.open)
                segs: tuple[str, ...] = ("pre",)
            else:
                segs = session_segments("rth", p)
            fp = self._prefix_grid(symbol, td, until, segs)
            used.append(t)
            src = self._bars(symbol, td).source_bar
            as_of = until
        elif profile_type == "anchored":
            anchor = kw.get("anchor", "auto")
            a_ns = self._anchor(symbol, t, anchor)
            until = self._until(td, kw.get("until"), td.start)  # 缺省只用之前的完整交易日
            if a_ns is None:
                fp, as_of = vp.FineProfile.empty(), until
            else:
                a_day = self.cal.trading_date_of(a_ns) or t
                tds = [d for d in self.store.days_between(symbol, a_day, t) if d.day < t]
                parts = []
                segs = session_segments("rth", p)
                for d in tds:
                    if d.day == a_day and a_ns > d.open:
                        # 锚点在盘中：那天只取锚点以后的部分（那天已完整收盘）
                        f = self._prefix_grid(symbol, d, d.end, segs, since=a_ns)
                        parts.append(f.split_adjusted(self.store.factor(symbol, d.day, t)))
                        used.append(d.day)
                    else:
                        parts.append(add_days([d]))
                if until > td.open:
                    parts.append(self._prefix_grid(symbol, td, until, segs, since=a_ns))
                    used.append(t)
                fp, as_of = vp.merge(parts), until
            a_txt = from_ns(a_ns).strftime("%Y-%m-%d %H:%M") if a_ns else None
            kw = {"anchor": str(anchor), "anchor_time": a_txt}
        else:
            raise ValueError(f"未知的 Profile 类型 {profile_type!r}")
        return WindowProfile(symbol, t, profile_type, dict(kw), fp, as_of, tuple(used), src)

    def _until(self, td: TradingDay, until: Any, default: int) -> int:
        if until is None:
            return default
        if isinstance(until, int | np.integer):
            return int(until)
        if isinstance(until, str):
            until = time.fromisoformat(until)
        if isinstance(until, datetime):
            return int(until.timestamp() * 1e9)
        return ny_to_ns(td.day, until)

    def anchors(self, symbol: str, start: str | date, end: str | date) -> list[date]:
        """自动锚点：常规时段开盘价相对前一日收盘价跳空 ≥ anchor_gap_adr × ADR 的日子。"""
        out = []
        for td in self.store.days_between(symbol.upper(), as_date(start), as_date(end)):
            g = self.store.day(symbol.upper(), td)
            if g is None:
                continue
            pc, adr = self.prev_close(symbol, td.day), self.adr(symbol, td.day)
            if adr > 0 and abs(g.stats.rth_open - pc) >= self.p.anchor_gap_adr * adr:
                out.append(td.day)  # nan 的比较为假
        return out

    def _anchor(self, symbol: str, t: date, anchor: Any) -> int | None:
        if anchor == "auto":
            back = self.store.days_before(symbol, t, self.p.anchor_lookback)
            if not back:
                return None
            found = self.anchors(symbol, back[0].day, back[-1].day)
            return self.cal.trading_day(found[-1]).open if found else None
        if isinstance(anchor, str) and len(anchor) > 10:
            dt = datetime.fromisoformat(anchor)
            dt = dt if dt.tzinfo else dt.replace(tzinfo=NY)
            return int(dt.timestamp() * 1e9)
        return self.cal.trading_day(as_date(anchor)).open

    # ---------- 关键价位 ----------

    def levels_of(self, w: WindowProfile) -> dict[str, Any]:
        """一个窗口的关键价位（规范 5.2 的一行）。"""
        adr = self.adr(w.symbol, w.day)
        ref = self.prev_close(w.symbol, w.day)
        size = self.bin_size(adr, ref)
        lv = self._levels(w.fine, size, ref)
        return _row(w.symbol, w.day, w.profile_type, w.params, w.as_of, size, adr, lv) | {
            "n_days": len(w.days),
            "source_bar": w.source_bar,
        }

    def _levels(self, fp: vp.FineProfile, size: int, ref: float) -> vp.Levels | None:
        p = self.p
        return vp.levels(
            fp, size, value_area_pct=p.value_area_pct, ref_price=ref, smooth_bins=p.smooth_bins,
            hvn_min_frac=p.hvn_min_frac, lvn_max_frac=p.lvn_max_frac,
        )  # fmt: skip

    def default_windows(self) -> list[tuple[str, dict[str, Any]]]:
        p = self.p
        return (
            [("daily", {})]
            + [("composite", {"n": n}) for n in p.composite_days]
            + [("decay", {"half_life": h}) for h in p.decay_half_lives]
            + [("anchored", {"anchor": "auto"}), ("premarket", {}), ("postmarket", {})]
        )

    def get_levels(self, symbol: str, day: str | date, profile_type: str = "daily",
                   **kw: Any) -> dict[str, Any]:  # fmt: skip
        """T 开盘前可用的关键价位（premarket 截至开盘）。"""
        return self.levels_of(self.window(symbol, day, profile_type, **kw))

    def levels_table(
        self,
        symbols: list[str] | str,
        start: str | date,
        end: str | date,
        windows: list[tuple[str, dict[str, Any]]] | None = None,
    ) -> pd.DataFrame:
        """每个 (证券, 交易日, Profile 类型, 参数) 一行（规范 5.2）。"""
        syms = [symbols] if isinstance(symbols, str) else list(symbols)
        rows = []
        for s in syms:
            for td in self.store.days_between(s.upper(), as_date(start), as_date(end)):
                for kind, kw in windows or self.default_windows():
                    rows.append(self.get_levels(s, td.day, kind, **kw))
        return pd.DataFrame(rows)

    # ---------- developing（规范 4.2）----------

    def get_developing(
        self, symbol: str, day: str | date, until: Any = None, step_min: int | None = None
    ) -> pd.DataFrame:
        """当天开盘到 until（缺省收盘）每 step 分钟一行：dPOC、dVAH、dVAL、累计成交量、是否稳定。

        每一步只用该时刻之前已收盘的 bar；已经离开清洗窗口的 bar 增量累加，不重算全天。"""
        symbol = symbol.upper()
        td = self.td(day)
        end = self._until(td, until, td.close)
        step = (step_min or self.p.developing_step_min) * MIN_NS
        adr = self.adr(symbol, td.day)
        ref = self.prev_close(symbol, td.day)
        size = self.bin_size(adr, ref)
        segs = session_segments("rth", self.p)
        db = self._bars(symbol, td)
        # 只用 1 秒 bar 时增量累加：已离开清洗窗口的 bar 加进 settled，每步只重算最后 half 根
        incremental = self.p.source == "1s"
        settled = vp.FineProfile.empty()
        n_set = 0
        rows = []
        t = td.open + step
        while t <= end:
            if incremental:
                n_new = max(n_set, db.settled(t))
                if n_new > n_set:
                    b, seg, _ = db.full(n_set, n_new)
                    settled = vp.merge([settled, *grids(b, seg, self.p, segs)[0].values()])
                    n_set = n_new
                b, seg = db.prefix(t, n_set)
                fp = vp.merge([settled, *grids(b, seg, self.p, segs)[0].values()])
            else:
                fp = self._prefix_grid(symbol, td, t, segs)
            lv = self._levels(fp, size, ref)
            rows.append(
                {
                    "symbol": symbol,
                    "date": td.day,
                    "time": from_ns(t),
                    "dpoc": lv.poc if lv else math.nan,
                    "dvah": lv.vah if lv else math.nan,
                    "dval": lv.val if lv else math.nan,
                    "cum_volume": fp.total,
                    "stable": t - td.open >= self.p.developing_min_minutes * MIN_NS,
                }
            )
            t += step
        return pd.DataFrame(rows)

    # ---------- 任意区间（研究用）----------

    def get_profile(
        self,
        symbol: str,
        start: str | date,
        end: str | date,
        bin_size: float | None = None,
        session: str | None = None,
    ) -> RangeProfile:
        """start ~ end（含两端）完整交易日的分布，end 当天的价格口径。

        bin_size（美元）缺省按 end 收盘时的 ADR。"""
        symbol = symbol.upper()
        e = as_date(end)
        tds = self.store.days_between(symbol, as_date(start), e)
        fp = vp.merge(self._day_grid(symbol, d, e, session) for d in tds)
        adr = self.store.adr_through(symbol, e) if self.store.has_data(symbol, e) else math.nan
        g = self.store.day(symbol, tds[-1]) if tds else None
        ref = g.stats.rth_close / self.store.factor(symbol, tds[-1].day, e) if g else math.nan
        size = round(bin_size * 100) if bin_size else self.bin_size(adr, ref)
        b = vp.rebin(fp, size)
        idx = np.arange(len(b.vol))
        bins = pd.DataFrame(
            {
                "price_low": b.low(idx),
                "price_high": b.high(idx),
                "price_mid": b.mid(idx),
                "volume": b.vol,
            }  # fmt: skip
        )
        pc = math.nan
        if tds:  # 区间第一天的前一日收盘价，换算到 end 的口径
            pc = self.prev_close(symbol, tds[0].day) / self.store.factor(symbol, tds[0].day, e)
        return RangeProfile(
            bins, self._levels(fp, size, pc), fp, size / 100, adr, tuple(d.day for d in tds)
        )

    def day_levels(
        self, symbol: str, day: str | date, session: str | None = None
    ) -> dict[str, Any]:
        """某一天自己的 Profile（研究用，用当天收盘后的 ADR 定格子），当天价格口径。"""
        d = as_date(day)
        r = self.get_profile(symbol, d, d, session=session)
        size = round(r.bin_size * 100)
        return _row(symbol.upper(), d, "day", {}, self.td(d).end, size, r.adr, r.levels)

    # ---------- naked POC（规范 4.3）----------

    def naked_pocs(self, symbol: str, start: str | date, end: str | date) -> pd.DataFrame:
        """start ~ end 每天的 POC（前一日 Profile 的定义，当天价格），以及之后首次被触及的日期和时刻
        （清洗后的常规时段 1 秒 bar，从下一个交易日开始，查到 end 为止）。"""
        symbol = symbol.upper()
        tds = self.store.days_between(symbol, as_date(start), as_date(end))
        pending: list[dict[str, Any]] = []
        out: list[dict[str, Any]] = []
        for td in tds:
            if pending:
                db = self._bars(symbol, td)
                b, seg, _ = db.full()
                m = np.isin(seg, [SEG_CODE[s] for s in ("open_cross", "rth", "close_cross")])
                m &= b.ts < td.close
                lo, hi, ts = b.low[m], b.high[m], b.ts[m]
                for r in list(pending):
                    px = r["poc"] / self.store.factor(symbol, r["poc_date"], td.day)
                    hit = np.flatnonzero((lo <= px) & (hi >= px))
                    if len(hit):
                        r["touched_date"] = td.day
                        r["touched_time"] = from_ns(int(ts[hit[0]]))
                        pending.remove(r)
            nxt = self.cal.next_trading_day(td.day)
            w = self.window(symbol, nxt, "daily")
            lv = self.levels_of(w)
            if math.isfinite(lv["poc"]):
                poc = lv["poc"] * self.store.factor(symbol, td.day, nxt)
                r = {"symbol": symbol, "poc_date": td.day, "poc": poc,
                     "touched_date": None, "touched_time": None}  # fmt: skip
                out.append(r)
                pending.append(r)
        return pd.DataFrame(out, columns=["symbol", "poc_date", "poc", "touched_date",
                                          "touched_time"])  # fmt: skip

    def naked_at(self, symbol: str, day: str | date) -> pd.DataFrame:
        """T 开盘时，过去 naked_lookback 个交易日中尚未被触及的 POC；poc_now 为 T 的价格口径。"""
        t = as_date(day)
        back = self.store.days_before(symbol.upper(), t, self.p.naked_lookback)
        if not back:
            return pd.DataFrame()
        last = self.store.days_before(symbol.upper(), t, 1)[0].day
        tab = self.naked_pocs(symbol, back[0].day, last)
        tab = select_rows(tab, tab["touched_date"].isna()).copy()
        tab["poc_now"] = [
            px / self.store.factor(symbol.upper(), d, t)
            for px, d in zip(tab["poc"], tab["poc_date"], strict=True)
        ]
        return tab

    # ---------- 方法对比（规范 2.1、第 6 节）----------

    def compare(
        self, symbols: list[str] | str, start: str | date, end: str | date, other: ProfileParams
    ) -> pd.DataFrame:
        """同一批日子，本实例参数与 other 参数各算一次每天自己的 Profile。

        d_poc_adr 等为 POC / VAH / VAL 的差（other − 本实例，ADR 倍数）。"""
        o = VolumeProfile(other, self.history)
        syms = [symbols] if isinstance(symbols, str) else list(symbols)
        rows = []
        for s in syms:
            for td in self.store.days_between(s.upper(), as_date(start), as_date(end)):
                a, b = self.day_levels(s, td.day), o.day_levels(s, td.day)
                adr = a["adr"]
                row = {"symbol": s.upper(), "date": td.day, "adr": adr}
                for k in ("poc", "vah", "val"):
                    row[f"{k}_a"], row[f"{k}_b"] = a[k], b[k]
                    row[f"d_{k}_adr"] = (b[k] - a[k]) / adr if adr > 0 else math.nan
                rows.append(row)
        return pd.DataFrame(rows)


def _row(
    symbol: str, day: date, kind: str, params: dict[str, Any], as_of: int, size: int, adr: float,
    lv: vp.Levels | None,
) -> dict[str, Any]:  # fmt: skip
    nan = math.nan
    return {
        "symbol": symbol,
        "date": day,
        "profile_type": kind,
        "params": json.dumps(params, ensure_ascii=False, default=str),
        "as_of": from_ns(as_of),
        "bin_size": size / 100,
        "adr": adr,
        "poc": lv.poc if lv else nan,
        "vah": lv.vah if lv else nan,
        "val": lv.val if lv else nan,
        "profile_high": lv.profile_high if lv else nan,
        "profile_low": lv.profile_low if lv else nan,
        "total_volume": lv.total_volume if lv else 0.0,
        "hvn": [round(x, 4) for x in lv.hvn] if lv else [],
        "lvn": [round(x, 4) for x in lv.lvn] if lv else [],
        "hvn_significance": [round(x, 3) for x in lv.hvn_significance] if lv else [],
        "lvn_significance": [round(x, 3) for x in lv.lvn_significance] if lv else [],
        "va_width_adr": lv.va_width_adr(adr) if lv else nan,
        "poc_position": lv.poc_position if lv else nan,
        "n_hvn": lv.n_hvn if lv else 0,
        "volume_skew": lv.volume_skew if lv else nan,
    }


def _default_history() -> HistoryService:
    from trader.config import data_dir
    from trader.core.trading_calendar import TradingCalendar
    from trader.data.catalog import Catalog

    return HistoryService(data_dir(), TradingCalendar(), Catalog(data_dir() / "catalog.sqlite"))
