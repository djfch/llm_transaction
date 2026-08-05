"""配置模型与加载：config.yaml + watchlist.yaml + .env。

交易所 API key 只从 .env 读取，永不进入 API 响应（见 server 层 secrets/status）。
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

ROOT = Path(__file__).resolve().parent.parent

# LLM key 环境变量名白名单（.env 写入端 set_env_keys 与凭证 api_key_env 校验共用，防漂移）：
# 两个旧键名 + LLM_KEY_* 前缀；白名单外键名绝不接受（防死凭证、防把 GATE_API_KEY 当 LLM key 读）
ENV_KEY_WHITELIST = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
ENV_KEY_PREFIX = "LLM_KEY_"

# 思考程度统一档位：空串 = 不传任何参数（跟随模型默认）；off = 关闭；on = 开启；
# low/medium/high/xhigh/max = 强度档（越高思考越久、越费 token）
ThinkingEffort = Literal["", "off", "on", "low", "medium", "high", "xhigh", "max"]


class GateConfig(BaseModel):
    settle: str = "usdt"
    testnet_host: str = "https://api-testnet.gateapi.io/api/v4"
    live_host: str = "https://api.gateio.ws/api/v4"
    # SDK 内置 testnet WS 地址已 502 失效，必须单独可配；须与 settle 匹配
    testnet_ws_host: str = "wss://ws-testnet.gate.com/v4/ws/futures/usdt"


class CredentialConfig(BaseModel):
    """一条 LLM 凭证：厂商 + 模型 + base_url + key 对应的环境变量名。

    api_key_env 留空时按 `LLM_KEY_<name 大写，非字母数字换下划线>` 推导；
    显式填写时必须是大写环境变量名（`^[A-Z][A-Z0-9_]*$`）且在白名单内
    （ANTHROPIC_API_KEY / OPENAI_API_KEY / LLM_KEY_*，与 set_env_keys 写入端一致）。
    key 明文永不进本模型/配置文件，只存在 .env（见 config_io.set_env_keys）。
    """

    name: str
    provider: Literal["anthropic", "openai_compat", "openai_responses"] = "anthropic"
    model: str
    max_tokens: int = 4096
    openai_base_url: str = ""
    thinking_effort: ThinkingEffort = (
        ""  # 思考程度：空=跟随模型默认 / on / off / low / medium / high / xhigh / max
    )
    api_key_env: str = ""

    @model_validator(mode="after")
    def _fill_or_check_api_key_env(self) -> CredentialConfig:
        if not self.api_key_env:
            self.api_key_env = ENV_KEY_PREFIX + re.sub(r"[^A-Z0-9]", "_", self.name.upper())
        elif not re.fullmatch(r"[A-Z][A-Z0-9_]*", self.api_key_env):
            raise ValueError(
                f"api_key_env 必须是大写环境变量名（^[A-Z][A-Z0-9_]*$）: {self.api_key_env}"
            )
        elif self.api_key_env not in ENV_KEY_WHITELIST and not self.api_key_env.startswith(
            ENV_KEY_PREFIX
        ):
            raise ValueError(
                f"api_key_env 必须在白名单内（{' / '.join(ENV_KEY_WHITELIST)} / {ENV_KEY_PREFIX}*）"
                f": {self.api_key_env}"
            )
        return self


class LLMConfig(BaseModel):
    provider: str = "anthropic"  # anthropic / openai_compat / openai_responses
    model: str = "claude-sonnet-4-5"
    max_tokens: int = 4096
    openai_base_url: str = ""
    thinking_effort: ThinkingEffort = ""  # 思考程度（旧平铺字段；credentials 非空时由凭证接管）
    max_consecutive_failures: int = 3
    # 多凭证列表：为空时旧平铺字段生效（自动合成一条 default 凭证，零迁移）
    credentials: list[CredentialConfig] = []

    def resolve_credentials(self) -> list[CredentialConfig]:
        """返回生效凭证列表：非空校验 name 唯一后返回；为空用旧平铺字段合成 default。"""
        if self.credentials:
            names = [c.name for c in self.credentials]
            if len(set(names)) != len(names):
                raise ValueError(f"llm.credentials 存在重名凭证: {sorted(set(names))}")
            return list(self.credentials)
        env = "ANTHROPIC_API_KEY" if self.provider == "anthropic" else "OPENAI_API_KEY"
        return [
            CredentialConfig(
                name="default",
                provider=self.provider,
                model=self.model,
                max_tokens=self.max_tokens,
                openai_base_url=self.openai_base_url,
                thinking_effort=self.thinking_effort,
                api_key_env=env,
            )
        ]


class AgentBinding(BaseModel):
    """单个 agent 的凭证分配：值为凭证 name。"""

    credential: str = "default"


class AgentsConfig(BaseModel):
    """按 agent 分配凭证：trader（决策循环）/ reviewer（复盘 agent）。"""

    trader: AgentBinding = AgentBinding()
    reviewer: AgentBinding = AgentBinding()


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


class ReviewConfig(BaseModel):
    """复盘 agent：定时复盘的开关、间隔天数与触发时刻（本地时间）。"""

    enabled: bool = True
    # 到达间隔天数后的触发时刻（本地 HH:MM）
    daily_time: str = "03:00"
    # 复盘间隔天数：每隔 N 天复盘一次，区间为最近 N 天（默认 1 = 每天）
    interval_days: int = Field(default=1, ge=1, le=30)

    @model_validator(mode="after")
    def _check_daily_time(self) -> ReviewConfig:
        """daily_time 必须为 HH:MM（时 0-23、分 0-59）。"""
        parts = self.daily_time.split(":")
        if (
            len(parts) != 2
            or not all(p.isdigit() for p in parts)
            or not 0 <= int(parts[0]) <= 23
            or not 0 <= int(parts[1]) <= 59
        ):
            raise ValueError("daily_time 必须为 HH:MM 格式（时 0-23，分 0-59）")
        return self


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
    review: ReviewConfig = ReviewConfig()
    log: LogConfig = LogConfig()
    agents: AgentsConfig = AgentsConfig()

    @model_validator(mode="after")
    def _check_agent_credentials(self) -> Settings:
        """agent 引用的凭证必须存在（凭证重名也在此经 resolve_credentials 拦截）。"""
        names = {c.name for c in self.llm.resolve_credentials()}
        for agent, binding in (("trader", self.agents.trader), ("reviewer", self.agents.reviewer)):
            if binding.credential not in names:
                raise ValueError(
                    f"agents.{agent}.credential 引用了不存在的凭证: {binding.credential}"
                )
        return self

    def validate_mode(self) -> None:
        if self.mode not in ("paper", "testnet", "live"):
            raise ValueError(f"非法 mode: {self.mode}（可选 paper/testnet/live）")


class Watchlist(BaseModel):
    settle: str = "usdt"
    contracts: list[str]


# 指标短名单默认基线：文件缺失/首次运行时的兜底配置（去重后 1~8 个，键为小写字母/数字/下划线）
DEFAULT_INDICATOR_SHORTLIST = ["ema20", "ema50", "rsi14", "macd", "atr14", "oi"]


class IndicatorConfig(BaseModel):
    """指标短名单（indicator_config.yaml）：每轮注入执行 agent 上下文的技术指标键列表。

    本层只校验形状：去重后 1~8 个、键只允许小写字母/数字/下划线；
    键是否在指标注册表内（语义有效性）由 store 层按注入的 valid_keys 校验。
    """

    shortlist: list[str]

    @model_validator(mode="after")
    def _check_shortlist(self) -> IndicatorConfig:
        deduped: list[str] = []
        for key in self.shortlist:
            if key not in deduped:
                deduped.append(key)  # 去重保序
        if not 1 <= len(deduped) <= 8:
            raise ValueError(f"shortlist 去重后须 1~8 个，当前 {len(deduped)} 个")
        for key in deduped:
            if not re.fullmatch(r"[a-z0-9_]+", key):
                raise ValueError(f"指标键只允许小写字母/数字/下划线: {key!r}")
        self.shortlist = deduped
        return self


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


def load_indicator_config(path: Path | None = None) -> IndicatorConfig:
    """加载指标短名单；文件不存在返回默认基线（首次运行零配置可用）。"""
    target = path or ROOT / "indicator_config.yaml"
    if not target.exists():
        return IndicatorConfig(shortlist=list(DEFAULT_INDICATOR_SHORTLIST))
    return IndicatorConfig(**_load_yaml(target))
