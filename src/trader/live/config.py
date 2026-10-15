"""实盘版配置（DESIGN.md 12.2、12.3）：config/<profile>.yaml，pydantic 校验，未知字段报错。

启动检查 1：风控阈值必须全部显式填写（不使用默认值），否则拒绝启动。
模拟（local_paper、broker_paper）只允许模拟端口；实盘（live，阶段 7）只允许实盘端口，
并且必须写明账户号（U 开头）；策略源码哈希、上线清单在启动检查里核对（LiveRunner.preflight）。
策略执行方式：live 默认 confirm（人工逐笔确认开仓），只有在 live 配置里显式写 auto 才自动执行；
模拟默认 auto。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from trader.brokers.ibkr.api import LIVE_PORTS, PAPER_PORTS
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
    execution: Literal["auto", "confirm"] | None = Field(
        default=None, description="不写：live 为 confirm，模拟为 auto"
    )
    source_hash: str = Field(default="", description="live：启动时与策略源码核对，不一致拒绝启动")


class ConfirmOptions(BaseModel):
    """半自动确认（DESIGN.md 8.6）。"""

    model_config = ConfigDict(extra="forbid")

    timeout_s: int = Field(default=15, ge=1, description="超过这么久没确认，信号作废")
    max_price_move_pct: float = Field(
        default=0.5, gt=0, description="确认时最新价相对信号价偏离超过该百分比，信号作废"
    )
    confirm_exits: bool = Field(
        default=False, description="策略自己的减仓单也要确认（止损、收盘前平仓始终直接执行）"
    )


# 实盘上线清单（DESIGN.md 12.3 第 8 项）：live 配置里每一项都要由用户改成 true
GO_LIVE_ITEMS = {
    "gateway_precautions": "IB Gateway/TWS 的 API 预防性设置（单笔数量、金额上限）已设置",
    "account_limit": "实盘账户的资金上限已确认（以后改为资金有限的子账户）",
    "broker_paper_5_days": "broker_paper 连续运行 5 个交易日没有未处理的异常",
    "strategy_reviewed": "策略完成回测、样本外、回放、模拟盘各步并记录在策略说明里",
}


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
    confirm: ConfirmOptions = Field(default_factory=ConfirmOptions)
    go_live: dict[str, bool] = Field(default_factory=dict, description="实盘上线清单")
    control_host: str = Field(default="127.0.0.1", description="界面连接的地址，只允许本机")
    control_port: int = Field(default=0, ge=0, description="界面连接的端口；0 = 按运行方式取默认值")

    @property
    def ui_port(self) -> int:
        return self.control_port or {"local_paper": 8764, "broker_paper": 8765}.get(
            self.profile, 8766
        )

    @property
    def is_live(self) -> bool:
        return self.profile == "live"

    @property
    def execution(self) -> Literal["auto", "confirm"]:
        """策略的执行方式（没有策略时按 live 规则返回）。"""
        explicit = self.strategy.execution if self.strategy else None
        return explicit or ("confirm" if self.is_live else "auto")

    @model_validator(mode="after")
    def _check(self) -> LiveConfig:
        if self.is_live:
            if self.ibkr.port not in LIVE_PORTS:
                raise ValueError(
                    f"live 只允许实盘端口 {sorted(LIVE_PORTS)}，配置的是 {self.ibkr.port}"
                )
            if not self.ibkr.account.startswith("U"):
                raise ValueError("live 必须在 ibkr.account 写明实盘账户号（U 开头）")
            unknown = set(self.go_live) - set(GO_LIVE_ITEMS)
            if unknown:
                raise ValueError(f"go_live 里有不认识的项目：{sorted(unknown)}")
        elif self.ibkr.port not in PAPER_PORTS:
            raise ValueError(f"端口 {self.ibkr.port} 不是模拟账户端口 {sorted(PAPER_PORTS)}")
        if self.ibkr.account and self.ibkr.account.startswith("DU") == self.is_live:
            raise ValueError(f"账户 {self.ibkr.account} 与运行方式 {self.profile} 不符")
        if self.trade_extended and self.session != "extended":
            raise ValueError("trade_extended 需要 session: extended")
        return self


def go_live_missing(cfg: LiveConfig) -> list[str]:
    """上线清单里还没有标记完成的项目（说明文字）。"""
    return [text for key, text in GO_LIVE_ITEMS.items() if not cfg.go_live.get(key)]


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
