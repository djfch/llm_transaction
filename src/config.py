"""配置模型与加载：config.yaml + watchlist.yaml + .env。

交易所 API key 只从 .env 读取，永不进入 API 响应（见 server 层 secrets/status）。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

ROOT = Path(__file__).resolve().parent.parent


class GateConfig(BaseModel):
    settle: str = "usdt"
    testnet_host: str = "https://api-testnet.gateapi.io/api/v4"
    live_host: str = "https://api.gateio.ws/api/v4"


class LLMConfig(BaseModel):
    provider: str = "anthropic"  # anthropic / openai_compat
    model: str = "claude-sonnet-4-5"
    max_tokens: int = 4096
    openai_base_url: str = ""
    max_consecutive_failures: int = 3


class RiskConfig(BaseModel):
    max_position_pct: float = Field(default=0.30, gt=0, le=1)
    max_total_position_pct: float = Field(default=0.80, gt=0, le=1)
    max_leverage: int = Field(default=5, ge=1, le=100)
    daily_loss_limit: float = Field(default=0.10, gt=0, le=1)
    max_orders_per_day: int = Field(default=20, ge=1)
    max_deviation: float = Field(default=0.02, gt=0, le=1)
    kill_switch: bool = False


class SchedulerConfig(BaseModel):
    default_wake_minutes: int = Field(default=60, ge=1)
    min_wake_minutes: int = Field(default=5, ge=1)
    max_wake_minutes: int = Field(default=720, le=720)  # 上限 12 小时
    # 启动时是否自动开始 LLM 决策：默认 False——由用户在监控主页点击"启动 agent"才开始
    autostart: bool = False

    @model_validator(mode="after")
    def _check_wake_window(self) -> SchedulerConfig:
        """取值关系：min ≤ max ≤ 720，default 落在 [min, max] 之间。"""
        if self.min_wake_minutes > self.max_wake_minutes:
            raise ValueError("min_wake_minutes 不能大于 max_wake_minutes")
        if not self.min_wake_minutes <= self.default_wake_minutes <= self.max_wake_minutes:
            raise ValueError(
                "default_wake_minutes 必须在 min_wake_minutes 与 max_wake_minutes 之间"
            )
        return self


class PaperConfig(BaseModel):
    initial_equity: Decimal = Field(default=Decimal("10000"), gt=0)  # 金额一律 Decimal
    slippage: float = Field(default=0.0005, ge=0, le=0.1)


class NotifyConfig(BaseModel):
    telegram_enabled: bool = False


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(default=17577, ge=1, le=65535)


class AuditConfig(BaseModel):
    dir: str = "logs/audit"


class LogConfig(BaseModel):
    dir: str = "logs"
    level: str = "INFO"


class Settings(BaseModel):
    """config.yaml 的完整模型。"""

    mode: str = "paper"  # paper / testnet / live
    gate: GateConfig = GateConfig()
    llm: LLMConfig = LLMConfig()
    risk: RiskConfig = RiskConfig()
    scheduler: SchedulerConfig = SchedulerConfig()
    paper: PaperConfig = PaperConfig()
    notify: NotifyConfig = NotifyConfig()
    server: ServerConfig = ServerConfig()
    audit: AuditConfig = AuditConfig()
    log: LogConfig = LogConfig()

    def validate_mode(self) -> None:
        if self.mode not in ("paper", "testnet", "live"):
            raise ValueError(f"非法 mode: {self.mode}（可选 paper/testnet/live）")


class Watchlist(BaseModel):
    settle: str = "usdt"
    contracts: list[str]


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_settings(config_path: Path | None = None) -> Settings:
    settings = Settings(**_load_yaml(config_path or ROOT / "config.yaml"))
    settings.validate_mode()
    return settings


def load_watchlist(path: Path | None = None) -> Watchlist:
    return Watchlist(**_load_yaml(path or ROOT / "watchlist.yaml"))
