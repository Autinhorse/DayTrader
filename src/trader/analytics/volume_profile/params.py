"""Volume Profile 参数（规范各节的默认值）。notebook 的参数单元格里可以逐项覆盖：

ProfileParams(distribution="close", bin_adr_frac=0.05)
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING, Literal

from trader.core.volume_profile import BinMode, Distribution

if TYPE_CHECKING:
    from trader.analytics.cleaning import BadWindow

Source = Literal["1s", "1m", "5m"]
SessionSel = Literal["rth", "pre", "post", "eth"]

# 计算逻辑改变、需要重建缓存时加一
CODE_VERSION = 1


@dataclass(frozen=True, slots=True)
class ProfileParams:
    # ---- 构建每日细网格（改变这些会使用另一份缓存）----
    distribution: Distribution = "uniform"  # 规范 2.1
    # bar 粒度：1s（默认）；1m / 5m 由清洗后的 1 秒 bar 合成，用于精度对比。
    # 没有 1 秒数据的日子一律退回 Massive 官方 1 分钟 bar
    source: Source = "1s"
    min_bar_volume: float = 0  # 常规时段：成交量低于此值的 bar 不参与
    min_bar_volume_eth: float = 100  # 盘前盘后
    print_filter_adr: float = 0.3  # 离群成交阈值（ADR 倍数，决策 0011）
    clean_half: int = 30  # 离群判断用前后各多少根 1 秒 bar 的中位数
    bad_windows: tuple[BadWindow, ...] | None = None  # None = 读 config/bad_data_windows.yaml
    # ---- 读取与分析 ----
    session: SessionSel = "rth"  # rth 9:30–16:00；pre 4:00–9:30；post 16:00–20:00；eth = pre + post
    exclude_open_cross: bool = False  # 剔除 9:30:00 那一秒
    exclude_close_cross: bool = True  # 剔除收盘前最后一秒和收盘那一秒（收盘竞价）
    bin_mode: BinMode = "adr"
    bin_adr_frac: float = 0.02  # 格子 = ADR × 0.02（一天约 50 个格子）
    bin_pct: float = 0.0005
    bin_fixed: float = 0.05
    adr_days: int = 21  # ADR：过去 21 个常规交易日（不含提前收盘日）清洗后振幅的中位数
    adr_min_days: int = 5  # 不足这么多天时 ADR 为空，格子改用 pct 模式
    value_area_pct: float = 0.7
    smooth_bins: int = 3
    hvn_min_frac: float = 0.3
    lvn_max_frac: float = 0.3
    # ---- 时间窗口 ----
    developing_step_min: int = 1
    developing_min_minutes: int = 15  # 开盘后这么多分钟以内的价值区标记为不稳定
    composite_days: tuple[int, ...] = (3, 5, 10, 20)
    decay_half_lives: tuple[float, ...] = (3, 5)
    decay_max_half_lives: float = 10  # 衰减 Profile 最多回看这么多个半衰期
    anchor_gap_adr: float = 1.0  # 自动锚点：开盘跳空 ≥ 1 ADR
    anchor_lookback: int = 60  # 自动锚点最多回看多少个交易日
    naked_lookback: int = 20

    def with_(self, **kw: object) -> ProfileParams:
        return replace(self, **kw)  # type: ignore[arg-type]

    def build_key(self) -> str:
        """每日细网格缓存的目录名：构建参数不同，缓存分开。"""
        windows = [
            (str(w.day), str(w.start), str(w.end), list(w.symbols)) for w in self.bad_windows or ()
        ]
        payload = {
            "v": CODE_VERSION,
            "distribution": self.distribution,
            "source": self.source,
            "mbv": self.min_bar_volume,
            "mbv_eth": self.min_bar_volume_eth,
            "pf": self.print_filter_adr,
            "half": self.clean_half,
            "adr_days": self.adr_days,
            "windows": windows,
        }
        h = hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:8]
        return f"{self.distribution}-{self.source}-{h}"

    def describe(self) -> dict[str, object]:
        d = asdict(self)
        d.pop("bad_windows", None)
        return d
