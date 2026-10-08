"""配置文件的读取与校验（DESIGN.md 12.2）。未知字段报错。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, field_validator


class SymbolConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    # 以下字段在后续阶段使用：半价差默认值（阶段 3）、实盘订阅方式（阶段 6）、是否允许做空（阶段 3）
    half_spread: float | None = None
    feed: Literal["tick", "snapshot"] = "snapshot"
    shortable: bool | None = None

    @field_validator("symbol")
    @classmethod
    def _upper(cls, v: str) -> str:
        v = v.strip().upper()
        if not v:
            raise ValueError("symbol 不能为空")
        return v


class Universe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbols: list[SymbolConfig]

    @property
    def names(self) -> list[str]:
        return [s.symbol for s in self.symbols]


def project_dir() -> Path:
    """项目根目录：环境变量 TRADER_HOME；否则从当前目录向上找有 config/universe.yaml 的目录
    （notebook 放在项目里任何位置都能用）；都找不到就用当前目录。"""
    env = os.environ.get("TRADER_HOME")
    if env:
        return Path(env)
    cwd = Path.cwd()
    for d in (cwd, *cwd.parents):
        if (d / "config" / "universe.yaml").exists():
            return d
    return cwd


def data_dir() -> Path:
    return Path(os.environ.get("TRADER_DATA_DIR") or project_dir() / "data")


def load_universe(path: Path | None = None) -> Universe:
    path = path or project_dir() / "config" / "universe.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw["symbols"] = [{"symbol": s} if isinstance(s, str) else s for s in raw.get("symbols", [])]
    return Universe.model_validate(raw)
