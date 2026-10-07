"""成交条件规则：哪些成交更新开高低收、哪些计入成交量（DESIGN.md 5.2、6.1）。

规则表来自 Massive 的 /v3/reference/conditions（SIP 官方的 consolidated 更新规则），
保存在 config/trade_conditions.json。成交上的条件代码是 SIP 的单字符代码，同一个字符在
CTA（纽交所/Arca 等上市）和 UTP（纳斯达克上市）两条行情线上含义可能不同，所以按行情线查表。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import polars as pl

Tape = Literal["CTA", "UTP"]

# 规则表里没有的代码按普通成交处理
_DEFAULT = (True, True, True)


@dataclass(frozen=True, slots=True)
class Rule:
    updates_high_low: bool
    updates_open_close: bool
    updates_volume: bool


class TradeRules:
    def __init__(
        self, table: dict[tuple[str, str], Rule], overrides: dict[str, Rule] | None = None
    ):
        self._table = table
        self._overrides = overrides or {}

    @classmethod
    def from_massive(cls, results: list[dict[str, Any]]) -> TradeRules:
        table: dict[tuple[str, str], Rule] = {}
        for r in results:
            if r.get("type") != "sale_condition":
                continue
            u = r.get("update_rules", {}).get("consolidated")
            if not u:
                continue
            rule = Rule(u["updates_high_low"], u["updates_open_close"], u["updates_volume"])
            for tape, code in (r.get("sip_mapping") or {}).items():
                table[(tape, code)] = rule
        return cls(table)

    @classmethod
    def load(cls, path: Path) -> TradeRules:
        return cls.from_massive(json.loads(path.read_text(encoding="utf-8"))["results"])

    def with_overrides(self, overrides: dict[str, Rule]) -> TradeRules:
        """按代码覆盖规则（所有行情线），用于试验，例如让 Form T 更新价格。"""
        return TradeRules(self._table, {**self._overrides, **overrides})

    def rule(self, tape: Tape, codes: str) -> Rule:
        """一笔成交的规则：多个条件代码时取交集（任何一个代码不更新就不更新）。"""
        hl, oc, vol = _DEFAULT
        for code in codes:
            if code in (" ", "@"):  # 空格分隔；@ 是常规成交
                continue
            r = self._overrides.get(code) or self._table.get((tape, code))
            if r is None:
                r = self._table.get(("FINRA_TDDS", code))
            if r is None:
                continue
            hl, oc, vol = (
                hl and r.updates_high_low,
                oc and r.updates_open_close,
                vol and r.updates_volume,
            )
        return Rule(hl, oc, vol)


def bars_from_trades(
    trades: pl.DataFrame, rules: TradeRules, tape: Tape, size_ns: int
) -> pl.DataFrame:
    """trades: ts, price, size, cond → 按规则生成 bar（ts_start, open, high, low, close, volume）。

    开收盘取更新开收盘的成交；高低取更新高低的成交；成交量取计入成交量的成交。
    区间内没有任何更新价格的成交就没有 bar。
    """
    conds = trades["cond"].fill_null("").unique().to_list()
    lut = {c: rules.rule(tape, c) for c in conds}
    flags = pl.DataFrame(
        {
            "cond": list(lut),
            "hl": [r.updates_high_low for r in lut.values()],
            "oc": [r.updates_open_close for r in lut.values()],
            "vol": [r.updates_volume for r in lut.values()],
        },
        schema={"cond": pl.String, "hl": pl.Boolean, "oc": pl.Boolean, "vol": pl.Boolean},
    )
    t = (
        trades.with_columns(pl.col("cond").fill_null(""))
        .join(flags, on="cond", how="left")
        .with_columns((pl.col("ts") // size_ns * size_ns).alias("ts_start"))
        .sort("ts", maintain_order=True)
    )
    price = pl.col("price")
    return (
        t.group_by("ts_start", maintain_order=True)
        .agg(
            price.filter(pl.col("oc")).first().alias("open"),
            price.filter(pl.col("hl")).max().alias("high"),
            price.filter(pl.col("hl")).min().alias("low"),
            price.filter(pl.col("oc")).last().alias("close"),
            pl.col("size").filter(pl.col("vol")).sum().cast(pl.Float64).alias("volume"),
        )
        .filter(pl.col("high").is_not_null() | pl.col("open").is_not_null())
        .with_columns(
            pl.coalesce("open", "high").alias("open"),
            pl.coalesce("close", "low").alias("close"),
            pl.coalesce("high", "open").alias("high"),
            pl.coalesce("low", "open").alias("low"),
        )
        .sort("ts_start")
    )
