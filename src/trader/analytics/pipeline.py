"""把读取、每日指标、汇总、打分串起来，得到一份报表的全部表格。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from trader.analytics import aggregate, scoring
from trader.analytics.daily_metrics import compute_day
from trader.analytics.loader import load_symbol
from trader.analytics.params import ReportParams, ksuffix
from trader.data.history import HistoryService

NY = "America/New_York"


def _d(x: str | date) -> date:
    return x if isinstance(x, date) else date.fromisoformat(x)


def _history() -> HistoryService:
    from trader.config import data_dir
    from trader.core.trading_calendar import TradingCalendar
    from trader.data.catalog import Catalog

    return HistoryService(data_dir(), TradingCalendar(), Catalog(data_dir() / "catalog.sqlite"))


@dataclass
class VolReport:
    params: ReportParams
    start: date
    end: date
    tickers: list[str]  # 用户输入（不含参照证券）
    benchmarks: list[str]
    missing_symbols: list[str]  # 本地完全没有数据
    daily: pd.DataFrame  # 每只证券每个交易日一行
    swings: pd.DataFrame  # 摆动明细
    buckets: pd.DataFrame  # 时段指标，每只证券、每天、每个时间桶一行
    excluded: pd.DataFrame  # 未参与汇总的日子及原因
    stats: pd.DataFrame
    medians: pd.DataFrame
    ratios: pd.DataFrame
    extremes: pd.DataFrame
    bucket_rv_share: pd.DataFrame
    ranking: pd.DataFrame
    notes: list[str] = field(default_factory=list)
    history: HistoryService | None = field(default=None, repr=False)  # 单日详情图要重新读 bar

    @property
    def symbols(self) -> list[str]:
        return list(self.ranking.index)

    @property
    def flagged_halts(self) -> pd.DataFrame:
        d = self.daily
        cols = ["symbol", "date", "max_gap_min", "missing_ratio"]
        return d.loc[d["possible_halt"], cols].reset_index(drop=True)

    def detail_tickers(self) -> list[str]:
        r = self.ranking
        r = r[~r["benchmark"]]
        return [str(s) for s in list(r.index)[: self.params.detail_count]]

    def export(self, out_dir: str | Path | None = None) -> Path:
        """排名总表、每日指标导出 CSV（Excel 可直接打开，UTF-8 带 BOM），摆动明细导出 Parquet。"""
        if out_dir is None:
            from trader.config import project_dir

            out_dir = project_dir() / "reports" / f"intraday_vol_{self.start}_{self.end}"
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        self.ranking.to_csv(out / "ranking.csv", encoding="utf-8-sig")
        self.daily.to_csv(out / "daily.csv", index=False, encoding="utf-8-sig")
        self.excluded.to_csv(out / "excluded_days.csv", index=False, encoding="utf-8-sig")
        sw = self.swings.copy()
        for c in ("start", "end", "confirm"):
            if c in sw.columns:
                sw[c] = sw[c].dt.tz_convert("UTC").dt.tz_localize(None)  # parquet 存 UTC
        sw.to_parquet(out / "swings.parquet", index=False)
        return out


def build_report(
    tickers: list[str] | str,
    start: str | date,
    end: str | date,
    params: ReportParams | None = None,
    history: HistoryService | None = None,
    **overrides: Any,
) -> VolReport:
    p = replace(params or ReportParams(), **overrides)
    if p.score_k not in p.swing_thresholds_atr:
        raise ValueError(
            f"score_k={p.score_k} 必须在 swing_thresholds_atr {p.swing_thresholds_atr} 里"
        )
    s, e = _d(start), _d(end)
    if e < s:
        raise ValueError("end_date 早于 start_date")
    if isinstance(tickers, str):
        tickers = tickers.replace(",", " ").split()
    user = list(dict.fromkeys(t.strip().upper() for t in tickers if t.strip()))
    bench = [b.upper() for b in p.benchmark_tickers]
    all_syms = list(dict.fromkeys(user + bench))
    h = history or _history()

    day_rows: list[dict] = []
    swing_rows: list[dict] = []
    bucket_rows: list[dict] = []
    excluded: list[dict] = []
    missing: list[str] = []
    for sym in all_syms:
        if not h.coverage(sym):
            missing.append(sym)
            continue
        sd = load_symbol(h, sym, s, e, p)
        for d in sd.no_data:
            excluded.append({"symbol": sym, "date": d, "reason": "本地无数据"})
        for day, inp in sorted(sd.inputs.items()):
            res = compute_day(inp, p)
            m = res.metrics
            early = day in sd.early_close
            atr_ok = math.isfinite(m["atr"])
            included = atr_ok and not (early and p.exclude_early_close)
            if not atr_ok:
                excluded.append({"symbol": sym, "date": day, "reason": "ATR 预热数据不足"})
            elif early and p.exclude_early_close:
                excluded.append({"symbol": sym, "date": day, "reason": "提前收盘"})
            row = {
                "symbol": sym,
                "date": day,
                "benchmark": sym in bench and sym not in user,
                "early_close": early,
                "possible_halt": m["max_gap_min"] > p.halt_gap_minutes,
                "atr_ok": atr_ok,
                "included": included,
                **m,
            }
            day_rows.append(row)
            for k, sws in res.swings.items():
                for sw in sws:
                    swing_rows.append(
                        {
                            "symbol": sym,
                            "date": day,
                            "k": k,
                            "direction": sw.direction,
                            "start": sw.start_ts,
                            "end": sw.end_ts,
                            "confirm": sw.confirm_ts,
                            "start_price": sw.start_price,
                            "end_price": sw.end_price,
                            "amplitude_pct": sw.amplitude_pct,
                            "amplitude_atr": sw.amplitude / m["atr"],
                            "minutes": sw.minutes,
                        }
                    )
            for b in res.buckets:
                bucket_rows.append({"symbol": sym, "date": day, "included": included, **b})

    daily = pd.DataFrame(day_rows)
    if daily.empty:
        raise ValueError("所选证券在这段时间里没有可用数据（缺数据的证券请先在数据管理里下载）")
    swings = pd.DataFrame(swing_rows)
    for c in ("start", "end", "confirm"):
        if c in swings.columns:
            swings[c] = pd.to_datetime(swings[c], unit="ns", utc=True).dt.tz_convert(NY)
    buckets = pd.DataFrame(bucket_rows)

    med = aggregate.medians(daily)
    ratios = aggregate.ratios_vs(med, aggregate.ratio_columns(p))
    scores = scoring.score(med, daily, p)
    sfx = ksuffix(p.score_k)
    key = [
        "range_pct",
        "range_atr",
        "body_ratio",
        f"swing_n_{sfx}",
        f"swing_avg_pct_{sfx}",
        f"swing_capturable_pct_{sfx}",
        "vwap_crosses",
        "missing_ratio",
        "dollar_vol",
    ]
    ranking = scores.join(med[key]).join(ratios)
    ranking.insert(5, "low_liquidity", ranking["dollar_vol"] < p.min_dollar_vol)
    ranking.insert(6, "benchmark", [sym in bench and sym not in user for sym in ranking.index])
    ranking.insert(7, "days", daily[daily["included"]].groupby("symbol").size())
    ranking = ranking.sort_values("score_total", ascending=False)
    ranking.index.name = "symbol"

    notes = []
    if len(ranking) < p.min_tickers_for_scores:
        notes.append(
            f"证券数量较少（{len(ranking)} 只，少于 {p.min_tickers_for_scores}），"
            "分数分档粗，请以原始指标为主"
        )
    if missing:
        notes.append("本地没有数据、未参与计算：" + "、".join(missing) + "（请先在数据管理里下载）")
    return VolReport(
        params=p,
        start=s,
        end=e,
        tickers=[t for t in user if t not in missing],
        benchmarks=[b for b in bench if b not in missing],
        missing_symbols=missing,
        daily=daily,
        swings=swings,
        buckets=buckets,
        excluded=pd.DataFrame(excluded, columns=["symbol", "date", "reason"]),
        stats=aggregate.stats(daily),
        medians=med,
        ratios=ratios,
        extremes=aggregate.extremes(daily),
        bucket_rv_share=aggregate.bucket_medians(buckets).reindex(ranking.index),
        ranking=ranking,
        notes=notes,
        history=h,
    )
