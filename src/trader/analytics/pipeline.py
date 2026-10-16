"""把读取、每日指标、异常日检测、汇总、打分串起来，得到一份报表的全部表格。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any, cast

import pandas as pd

from trader.analytics import aggregate, extended_hours, quality, scoring
from trader.analytics.daily_metrics import compute_day
from trader.analytics.extended_hours import eth_metrics
from trader.analytics.frames import corr
from trader.analytics.loader import load_symbol
from trader.analytics.params import ReportParams, ksuffix
from trader.data.corporate_actions import load_actions
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
    daily: pd.DataFrame  # 每只证券每个交易日一行（价格为复权价格）
    swings: pd.DataFrame  # 摆动明细；confirmed=False 的行是收盘时未确认的最后一段
    buckets: pd.DataFrame  # 时段指标，每只证券、每天、每个时间桶一行
    excluded: pd.DataFrame  # 未参与汇总的日子及原因
    stats: pd.DataFrame
    medians: pd.DataFrame
    means: pd.DataFrame
    ratios: pd.DataFrame
    extremes: pd.DataFrame
    bucket_rv_share: pd.DataFrame
    ranking: pd.DataFrame
    sensitivity: pd.DataFrame  # 摆动阈值敏感性，列为 (阈值, 字段)
    leveraged: pd.DataFrame  # 杠杆 ETF 与标的对照（leveraged_pairs 为空时是空表）
    eth_summary: pd.DataFrame  # 扩展时段参考（不参与打分）
    splits: pd.DataFrame  # 区间内的拆股
    compare: pd.DataFrame | None = None  # 多区间对比（compare_periods）
    compare_spearman: float = math.nan
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

    @property
    def anomalies(self) -> pd.DataFrame:
        d = self.daily
        k = ksuffix(self.params.score_k)
        cols = [
            "symbol", "date", "anomaly_reason", "range_pct", f"swing_n_{k}", "swing_density",
            "outlier_bars", "included",
        ]  # fmt: skip
        return d.loc[d["anomaly_flag"], cols].reset_index(drop=True)

    def detail_tickers(self) -> list[str]:
        r = self.ranking
        r = r[~r["benchmark"]]
        return [str(s) for s in list(r.index)[: self.params.detail_count]]

    def export(self, out_dir: str | Path | None = None) -> Path:
        """CSV（Excel 可直接打开，UTF-8 带 BOM）：排名总表、每日指标、未参与汇总的日子、异常日、
        阈值敏感性、杠杆对照、扩展时段汇总、多区间对比；摆动明细导出 Parquet。"""
        if out_dir is None:
            from trader.config import project_dir

            out_dir = project_dir() / "reports" / f"intraday_vol_{self.start}_{self.end}"
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        enc = "utf-8-sig"
        self.ranking.to_csv(out / "ranking.csv", encoding=enc)
        self.daily.to_csv(out / "daily.csv", index=False, encoding=enc)
        self.excluded.to_csv(out / "excluded_days.csv", index=False, encoding=enc)
        self.anomalies.to_csv(out / "anomaly_days.csv", index=False, encoding=enc)
        sens = self.sensitivity.copy()
        sens.columns = [f"k{a}_{b}" for a, b in sens.columns]
        sens.to_csv(out / "threshold_sensitivity.csv", encoding=enc)
        if not self.leveraged.empty:
            self.leveraged.to_csv(out / "leveraged_pairs.csv", encoding=enc)
        if not self.eth_summary.empty:
            self.eth_summary.to_csv(out / "extended_hours.csv", encoding=enc)
        if self.compare is not None:
            self.compare.to_csv(out / "period_compare.csv", encoding=enc)
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
    if p.score_k not in p.swing_thresholds_adr:
        raise ValueError(
            f"score_k={p.score_k} 必须在 swing_thresholds_adr {p.swing_thresholds_adr} 里"
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
    path_close: aggregate.PathClose = {}
    for sym in all_syms:
        if not h.coverage(sym):
            missing.append(sym)
            continue
        sd = load_symbol(h, sym, s, e, p)
        path_close[sym] = sd.path_close
        for d in sd.no_data:
            excluded.append({"symbol": sym, "date": d, "reason": "本地无数据"})
        for day, inp in sorted(sd.inputs.items()):
            res = compute_day(inp, p)
            m = res.metrics
            early = day in sd.early_close
            adr_ok = math.isfinite(m["adr"])
            if not adr_ok:
                excluded.append({"symbol": sym, "date": day, "reason": "ADR 数据不足"})
            elif early and p.exclude_early_close:
                excluded.append({"symbol": sym, "date": day, "reason": "提前收盘"})
            eth = (
                eth_metrics(sd.eth.get(day), m["open"], inp.pc, m["adr"])
                if p.include_extended_hours
                else {}
            )
            day_rows.append(
                {
                    "symbol": sym,
                    "date": day,
                    "benchmark": sym in bench and sym not in user,
                    "early_close": early,
                    "possible_halt": m["max_gap_min"] > p.halt_gap_minutes,
                    "adr_ok": adr_ok,
                    "included": adr_ok and not (early and p.exclude_early_close),
                    **m,
                    **eth,
                }
            )
            adr = m["adr"]
            for k, sws in res.swings.items():
                for sw in sws:
                    swing_rows.append(
                        _swing_row(sym, day, k, sw.direction, sw.start_ts, sw.end_ts,
                                   sw.confirm_ts, sw.start_price, sw.end_price, adr, True)
                    )  # fmt: skip
                leg = res.last_legs.get(k)
                if leg is not None:
                    d0 = 1 if leg.end_price >= leg.start_price else -1
                    swing_rows.append(
                        _swing_row(sym, day, k, d0, leg.start_ts, leg.end_ts, None,
                                   leg.start_price, leg.end_price, adr, False)
                    )  # fmt: skip
            for b in res.buckets:
                bucket_rows.append({"symbol": sym, "date": day, **b})

    daily = pd.DataFrame(day_rows)
    if daily.empty:
        raise ValueError("所选证券在这段时间里没有可用数据（缺数据的证券请先在数据管理里下载）")
    daily = quality.flag_anomalies(daily, p)
    if p.exclude_anomaly_days:
        drop = daily["anomaly_flag"] & daily["included"]
        for sym, day in zip(daily.loc[drop, "symbol"], daily.loc[drop, "date"], strict=True):
            excluded.append({"symbol": sym, "date": day, "reason": "异常日（已设置剔除）"})
        daily["included"] = daily["included"] & ~daily["anomaly_flag"]
    swings = pd.DataFrame(swing_rows)
    for c in ("start", "end", "confirm"):
        if c in swings.columns:
            swings[c] = pd.to_datetime(swings[c], unit="ns", utc=True).dt.tz_convert(NY)
    buckets = pd.DataFrame(bucket_rows)
    if not buckets.empty:
        inc = daily.set_index(["symbol", "date"])["included"]
        keys = pd.MultiIndex.from_frame(cast(pd.DataFrame, buckets[["symbol", "date"]]))
        buckets.insert(2, "included", inc.reindex(keys).to_numpy())

    med = aggregate.medians(daily)
    mean = aggregate.means(daily)
    days_ratio = {k: aggregate.capturable_days_ratio(daily, k) for k in p.swing_thresholds_adr}
    ratios, ratio_notes = aggregate.ratios_vs(med, mean, days_ratio[p.score_k], p.score_k)
    scores = scoring.score(med, mean, days_ratio[p.score_k], daily, p)
    sfx = ksuffix(p.score_k)
    key = pd.DataFrame(
        {
            "range_pct": med["range_pct"],
            "range_adr": med["range_adr"],
            "adr_pct": med["adr_pct"],
            "body_ratio": med["body_ratio"],
            f"swing_n_{sfx}": med[f"swing_n_{sfx}"],
            f"swing_n_{sfx}_mean": mean[f"swing_n_{sfx}"],
            f"swing_avg_pct_{sfx}": med[f"swing_avg_pct_{sfx}"],
            f"swing_capturable_pct_{sfx}": med[f"swing_capturable_pct_{sfx}"],
            f"swing_capturable_pct_{sfx}_mean": mean[f"swing_capturable_pct_{sfx}"],
            "capturable_days_ratio": days_ratio[p.score_k].reindex(med.index),
            "vwap_crosses": med["vwap_crosses"],
            "missing_ratio": med["missing_ratio"],
            "dollar_vol": med["dollar_vol"],
        }
    )
    ranking = scores.join(key).join(ratios)
    ranking.insert(5, "low_liquidity", ranking["dollar_vol"] < p.min_dollar_vol)
    ranking.insert(6, "benchmark", [sym in bench and sym not in user for sym in ranking.index])
    ranking.insert(7, "days", daily[daily["included"]].groupby("symbol").size())
    ranking = ranking.sort_values("score_total", ascending=False)
    ranking.index.name = "symbol"
    sens = scoring.sensitivity(med, mean, daily, p, days_ratio).reindex(ranking.index)
    lev, lev_notes = aggregate.leveraged_table(
        daily, med, mean, path_close, p.leveraged_pairs, p.score_k
    )

    notes = []
    if len(ranking) < p.min_tickers_for_scores:
        notes.append(
            f"证券数量较少（{len(ranking)} 只，少于 {p.min_tickers_for_scores}），"
            "分数分档粗，请以原始指标为主"
        )
    if missing:
        notes.append("本地没有数据、未参与计算：" + "、".join(missing) + "（请先在数据管理里下载）")
    notes += ratio_notes + lev_notes

    compare, rho = None, math.nan
    if p.compare_periods is not None:
        cs, ce = p.compare_periods
        other = build_report(user, cs, ce, replace(p, compare_periods=None), h)
        compare, rho = _compare(ranking, other.ranking, f"{s}~{e}", f"{_d(cs)}~{_d(ce)}")
    eth_sum = (
        extended_hours.summarize(daily).reindex(ranking.index)
        if p.include_extended_hours
        else pd.DataFrame()
    )
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
        means=mean,
        ratios=ratios,
        extremes=aggregate.extremes(daily, p.score_k),
        bucket_rv_share=aggregate.bucket_medians(buckets).reindex(ranking.index),
        ranking=ranking,
        sensitivity=sens,
        leveraged=lev,
        eth_summary=eth_sum,
        splits=_splits(h, [x for x in all_syms if x not in missing], s, e),
        compare=compare,
        compare_spearman=rho,
        notes=notes,
        history=h,
    )


def _swing_row(
    sym: str, day: date, k: float, direction: int, start: int, end: int, confirm: int | None,
    p0: float, p1: float, adr: float, confirmed: bool,
) -> dict:  # fmt: skip
    return {
        "symbol": sym,
        "date": day,
        "k": k,
        "confirmed": confirmed,
        "direction": direction,
        "start": start,
        "end": end,
        "confirm": confirm,
        "start_price": p0,
        "end_price": p1,
        "amplitude_pct": abs(p1 - p0) / p0 * 100,
        "amplitude_adr": abs(p1 - p0) / adr,
        "minutes": (end - start) / 60e9,
    }


def _compare(a: pd.DataFrame, b: pd.DataFrame, la: str, lb: str) -> tuple[pd.DataFrame, float]:
    """两个区间的综合得分和名次；Spearman 相关 = 两边都有的证券的得分名次的相关系数。"""
    t = pd.DataFrame(
        {
            f"score_{la}": a["score_total"],
            f"rank_{la}": a["score_total"].rank(ascending=False, method="min"),
            f"score_{lb}": b["score_total"],
            f"rank_{lb}": b["score_total"].rank(ascending=False, method="min"),
        }
    )
    t["rank_change"] = t[f"rank_{lb}"] - t[f"rank_{la}"]
    both = t.dropna(subset=[f"score_{la}", f"score_{lb}"])
    rho = math.nan
    if len(both) >= 3:
        rho = corr(both[f"score_{la}"], both[f"score_{lb}"], rank=True)
    return t.sort_values(f"rank_{la}"), rho


def _splits(h: HistoryService, syms: list[str], s: date, e: date) -> pd.DataFrame:
    acts = load_actions(h.data_dir).to_pandas()
    cols = ["symbol", "ex_date", "ratio"]
    if acts.empty:
        return pd.DataFrame(columns=cols)
    ex = pd.to_datetime(acts["ex_date"])
    m = (acts["kind"] == "split") & acts["symbol"].isin(syms)
    m &= (ex >= pd.Timestamp(s)) & (ex <= pd.Timestamp(e))
    return acts.loc[m, cols].reset_index(drop=True)
