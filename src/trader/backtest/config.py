"""回测配置（DESIGN.md 10.1），YAML 文件或字典，pydantic 校验，未知字段报错。

例：
    strategy: ema_cross
    params: {symbol: SPY, fast: 9, slow: 21, notional: 10000}
    start: 2026-09-01
    end: 2026-09-30
    session: rth            # 行情与策略周期：rth 只看常规时段；extended 含盘前盘后
    trade_extended: false   # 是否允许盘前盘后开仓（只能用 outside_rth 限价单）
    hold_post: false        # false：常规收盘前平仓；true：持仓到盘后，20:00 前平仓
    initial_cash: 100000
    sim: {half_spread_bps: {default: 2, SPY: 0.5}, impact_k: 10}
    risk: {max_order_usd: 20000, max_daily_loss_usd: 2000}

指标预热不需要单独配置：每个指标按自己声明的需求自动往前取数（DESIGN.md 6.5）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from trader.brokers.sim.matcher import SimConfig
from trader.oms.risk import RiskLimits


class BacktestConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="", description="实验名称，便于在列表中辨认")
    strategy: str
    params: dict[str, Any] = Field(default_factory=dict)
    start: date
    end: date
    session: Literal["rth", "extended"] = "rth"
    trade_extended: bool = False
    hold_post: bool = False
    initial_cash: float = Field(default=100_000, gt=0)
    symbols: list[str] = Field(
        default_factory=list, description="允许交易的标的；默认 config/universe.yaml 全部"
    )
    sim: SimConfig = Field(default_factory=lambda: SimConfig())
    risk: RiskLimits = Field(default_factory=lambda: RiskLimits())

    @model_validator(mode="after")
    def _check(self) -> BacktestConfig:
        if self.end < self.start:
            raise ValueError("end 不能早于 start")
        if self.trade_extended and self.session != "extended":
            raise ValueError("trade_extended 需要 session: extended")
        return self


def load_config(path: Path) -> BacktestConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return BacktestConfig.model_validate(raw)
