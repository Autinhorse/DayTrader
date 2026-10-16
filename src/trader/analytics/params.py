"""报表参数（规范 2.1）。全部是默认值，notebook 的参数单元格里可以逐项覆盖。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trader.analytics.cleaning import BadWindow


def ksuffix(k: float) -> str:
    """摆动阈值的字段名后缀：0.1 → "k010"，0.15 → "k015"，0.25 → "k025"。"""
    return f"k{round(k * 100):03d}"


@dataclass(frozen=True, slots=True)
class ReportParams:
    benchmark_tickers: tuple[str, ...] = ("SPY", "QQQ")
    path_bar_size: str = "5m"  # 路程、效率比、自相关、方差比用的周期
    adr_days: int = 21  # ADR：过去 adr_days 个常规交易日（不含提前收盘日）振幅的中位数
    swing_thresholds_adr: tuple[float, ...] = (0.1, 0.15, 0.25)
    score_k: float = 0.25  # 打分、时段指标、异常日检测用的摆动阈值
    vwap_cross_min_hold: int = 3  # 分钟（钟表时间）
    # 单边成本（基点），按当日实际开盘价（未复权）分档：(价格上限, 基点)，最后一档的上限为 None
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
    # 数据清洗（决策 0011）：1 秒 bar 偏离前后各 30 根收盘价中位数超过该 ADR 倍数的价格视为离群
    print_filter_adr: float = 0.3
    # 坏数据时段；None = 读 config/bad_data_windows.yaml
    bad_windows: tuple[BadWindow, ...] | None = None
    # 特殊日（规范 3.7）：Σ 摆动幅度 / 当日振幅（score_k 阈值）超过此值
    # 2026-07~09 的 22 只证券（0.25 ADR）：中位数约 2.9，99 分位约 7.5
    anomaly_swing_density: float = 7.5
    # swing_n 超过该证券中位数的倍数，且至少 anomaly_min_swings 次
    anomaly_swing_multiple: float = 4.0
    anomaly_min_swings: int = 10
    # 价格尖刺：清洗后 1 分钟最高/最低价偏离前后各 5 分钟收盘价中位数的 ADR 倍数
    anomaly_outlier_adr: float = 0.5
    anomaly_skip_open_min: int = 5  # 离群 bar 判断跳过开盘后几分钟
    exclude_data_issue_days: bool = False  # 只剔除"数据异常"日；行为特殊日从不剔除
    # 杠杆 ETF → (标的, 名义杠杆倍数)
    leveraged_pairs: dict[str, tuple[str, float]] = field(default_factory=dict)
    include_extended_hours: bool = True
    eth_min_bar_volume: float = 100  # 扩展时段成交量低于此值的 1 分钟 bar 不参与取价
    compare_periods: tuple[str, str] | None = None  # 另一个日期区间，做排名对比
    score_weights: dict[str, float] = field(
        default_factory=lambda: {
            "activity": 0.30,
            "rotation": 0.30,
            "swing_value": 0.25,
            "consistency": 0.15,
        }
    )
    # 活跃度：range_pct 中位数、rv_5min_pct 中位数
    activity_weights: dict[str, float] = field(
        default_factory=lambda: {"range_pct": 0.5, "rv_5min_pct": 0.5}
    )
    # 震荡度：swing_n 均值、1 − body_ratio 中位数、vwap_crosses 中位数（规范 5.2）
    rotation_weights: dict[str, float] = field(
        default_factory=lambda: {"swing_n": 0.5, "one_minus_body": 0.25, "vwap_crosses": 0.25}
    )
    # 摆动价值：swing_capturable_pct 均值、capturable_days_ratio、swing_avg_pct 中位数
    swing_value_weights: dict[str, float] = field(
        default_factory=lambda: {
            "swing_capturable_pct": 0.4,
            "capturable_days_ratio": 0.3,
            "swing_avg_pct": 0.3,
        }
    )
    detail_count: int = 3  # 单只证券详情默认取综合得分前几名
    review_plot_max: int = 16  # 数据异常日、行为特殊日各最多画几张核对图

    def cost_pct(self, price: float) -> float:
        """单边成本，百分数（5bp = 0.05）。"""
        for cap, bps in self.cost_tiers_bps:
            if cap is None or price < cap:
                return bps / 100
        return self.cost_tiers_bps[-1][1] / 100
