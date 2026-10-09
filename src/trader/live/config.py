"""实盘版配置（DESIGN.md 12.2、12.3）：config/<profile>.yaml，pydantic 校验，未知字段报错。

启动检查 1：风控阈值必须全部显式填写（不使用默认值），否则拒绝启动。
阶段 6 只允许 local_paper 和 broker_paper；live 配置在阶段 7 之前一律拒绝。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from trader.brokers.ibkr.api import PAPER_PORTS
from trader.brokers.sim.matcher import SimConfig
from trader.oms.risk import RiskLimits

Profile = Literal["local_paper", "broker_paper", "live"]


class ConfigError(ValueError):
    pass


class IbkrConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: int = 4002
    client_id: int = Field(default=21, description="与录制脚本（91）、TWS 手动连接区分")
    account: str = Field(default="", description="下单账户；只登录了一个账户时可留空")


class FeedOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tick_limit: int = Field(default=5, ge=0, description="账户的逐笔成交并发额度")
    grace_ms: int = Field(default=1000, ge=0, description="1 秒 bar 封口宽限（决策 0007）")
    watchdog_regular_s: int = Field(default=30, ge=1)
    watchdog_extended_s: int = Field(default=600, ge=1)
    record: bool = Field(default=True, description="实时 tick 和 1 秒 bar 落盘到 data/live/")


class StrategySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    params: dict[str, Any] = Field(default_factory=dict)
    execution: Literal["auto", "confirm"] = "auto"


class LiveConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: Profile
    ibkr: IbkrConnection = Field(default_factory=IbkrConnection)
    feed: FeedOptions = Field(default_factory=FeedOptions)
    symbols: list[str] = Field(default_factory=list, description="默认 config/universe.yaml 全部")
    strategy: StrategySpec | None = None
    session: Literal["rth", "extended"] = "extended"
    trade_extended: bool = False
    hold_post: bool = False
    initial_cash: float = Field(default=100_000, gt=0, description="local_paper 的模拟资金")
    sim: SimConfig = Field(default_factory=lambda: SimConfig())
    risk: RiskLimits
    max_clock_skew_ms: int = Field(default=2000, ge=0, description="本机与 IB 服务器时间偏差上限")

    @model_validator(mode="after")
    def _check(self) -> LiveConfig:
        if self.profile == "live":
            raise ValueError("阶段 6 不支持实盘（live）配置；实盘接入在阶段 7")
        if self.ibkr.port not in PAPER_PORTS:
            raise ValueError(f"端口 {self.ibkr.port} 不是模拟账户端口 {sorted(PAPER_PORTS)}")
        if self.trade_extended and self.session != "extended":
            raise ValueError("trade_extended 需要 session: extended")
        return self


def load_live_config(path: Path) -> LiveConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    risk = raw.get("risk") or {}
    missing = [k for k in RiskLimits.model_fields if k not in risk]
    if missing:
        raise ConfigError(
            f"{path.name}：风控阈值必须全部显式填写（DESIGN.md 12.3），缺少：{', '.join(missing)}"
        )
    try:
        return LiveConfig.model_validate(raw)
    except ValueError as exc:
        raise ConfigError(f"{path.name}：{exc}") from exc
