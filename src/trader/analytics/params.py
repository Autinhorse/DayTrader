"""报表参数（规范 2.1）。全部是默认值，notebook 的参数单元格里可以逐项覆盖。"""

from __future__ import annotations

from dataclasses import dataclass, field


def ksuffix(k: float) -> str:
    """摆动阈值的字段名后缀：0.25 → "k025"，0.5 → "k050"。"""
    return f"k{round(k * 100):03d}"


@dataclass(frozen=True, slots=True)
class ReportParams:
    benchmark_tickers: tuple[str, ...] = ("SPY", "QQQ")
    path_bar_size: str = "5m"  # 路程、效率比、自相关、方差比用的周期
    atr_days: int = 21
    # 某日之前至少要有 atr_days × 该倍数个交易日的数据，否则当天 ATR 记为空
    atr_warmup_factor: int = 3
    swing_thresholds_atr: tuple[float, ...] = (0.25, 0.5)
    score_k: float = 0.25  # 打分、时段指标用的摆动阈值
    vwap_cross_min_hold: int = 3  # 分钟（钟表时间）
    # 单边成本（基点），按当日开盘价分档：(价格上限, 基点)，最后一档的上限为 None
    cost_tiers_bps: tuple[tuple[float | None, float], ...] = (
        (20.0, 10.0),
        (100.0, 5.0),
        (None, 3.0),
    )
    halt_gap_minutes: int = 10
    exclude_early_close: bool = True
    min_dollar_vol: float = 20e6
    min_tickers_for_scores: int = 15
    time_buckets_min: int = 30
    score_weights: dict[str, float] = field(
        default_factory=lambda: {
            "activity": 0.30,
            "rotation": 0.30,
            "swing_value": 0.25,
            "consistency": 0.15,
        }
    )
    activity_weights: dict[str, float] = field(
        default_factory=lambda: {"range_pct": 0.5, "rv_5min_pct": 0.5}
    )
    # 震荡度：摆动次数、1 − body_ratio、VWAP 穿越（规范 5.2）
    rotation_weights: dict[str, float] = field(
        default_factory=lambda: {"swing_n": 0.5, "one_minus_body": 0.25, "vwap_crosses": 0.25}
    )
    swing_value_weights: dict[str, float] = field(
        default_factory=lambda: {"swing_capturable_pct": 0.6, "swing_avg_pct": 0.4}
    )
    detail_count: int = 3  # 单只证券详情默认取综合得分前几名

    def cost_pct(self, price: float) -> float:
        """单边成本，百分数（5bp = 0.05）。"""
        for cap, bps in self.cost_tiers_bps:
            if cap is None or price < cap:
                return bps / 100
        return self.cost_tiers_bps[-1][1] / 100

    @property
    def warmup_days(self) -> int:
        return self.atr_days * self.atr_warmup_factor
