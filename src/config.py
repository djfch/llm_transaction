"""配置模型与加载：config.yaml + watchlist.yaml + .env。

交易所 API key 只从 .env 读取，永不进入 API 响应（见 server 层 secrets/status）。
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

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
        """补全或校验 LLM 凭证 key 对应的环境变量名。

        api_key_env 留空时按 `LLM_KEY_<name 大写，非字母数字换下划线>` 自动推导；
        显式填写时校验大写格式与白名单（防死凭证、防误读交易所 key）。

        参数：无

        返回：
            CredentialConfig：校验通过后的当前模型实例

        异常：
            ValueError：api_key_env 不是大写环境变量名（^[A-Z][A-Z0-9_]*$），
                或不在白名单（ANTHROPIC_API_KEY / OPENAI_API_KEY / LLM_KEY_*）内时抛出
        """
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
        """返回生效凭证列表：非空校验 name 唯一后返回；为空用旧平铺字段合成 default。

        参数：无

        返回：
            list[CredentialConfig]，返回生效凭证列表：非空校验 name 唯一后返回；为空用旧平铺字段合成 default

        异常：
            ValueError，非空凭证列表存在重名 name 时抛出
        """
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
    """按 agent 分配凭证：trader（决策循环）/ reviewer（复盘 agent）/ researcher（研报 agent）。"""

    trader: AgentBinding = AgentBinding()
    reviewer: AgentBinding = AgentBinding()
    researcher: AgentBinding = AgentBinding()


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
        """取值关系：min ≤ max ≤ 720，default 落在 [min, max] 之间。

        参数：无

        返回：
            SchedulerConfig，取值关系：min ≤ max ≤ 720，default 落在 [min, max] 之间

        异常：
            ValueError，最小唤醒间隔大于最大值，或默认间隔不在最小值与最大值之间时抛出
        """
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
        """daily_time 必须为 HH:MM（时 0-23、分 0-59）。

        参数：无

        返回：
            ReviewConfig，daily_time 必须为 HH:MM（时 0-23、分 0-59）

        异常：
            ValueError，daily_time 不是合法 HH:MM 时抛出
        """
        parts = self.daily_time.split(":")
        if (
            len(parts) != 2
            or not all(p.isdigit() for p in parts)
            or not 0 <= int(parts[0]) <= 23
            or not 0 <= int(parts[1]) <= 59
        ):
            raise ValueError("daily_time 必须为 HH:MM 格式（时 0-23，分 0-59）")
        return self


MarketCode = Literal["XTKS", "XLON", "XNYS"]
CalendarCode = Literal["daily", "XTKS", "XLON", "XNYS"]


class MarketOpenSchedule(BaseModel):
    """市场开盘前调度：市场、提前分钟数与独立启停状态。"""

    id: Literal["asia_open", "europe_open", "us_open"]
    kind: Literal["market_open"] = "market_open"
    market: MarketCode
    enabled: bool = True
    lead_minutes: Literal[30] = 30

    @model_validator(mode="after")
    def _check_market_id(self) -> MarketOpenSchedule:
        """校验预设 ID 与市场代码一一对应。

        参数：无

        返回：
            MarketOpenSchedule：通过对应关系校验的预设调度

        异常：
            ValueError，预设 ID 与市场代码不匹配时抛出
        """
        expected = {"asia_open": "XTKS", "europe_open": "XLON", "us_open": "XNYS"}
        if expected[self.id] != self.market:
            raise ValueError(f"{self.id} 必须绑定市场 {expected[self.id]}")
        return self


class FixedTimeSchedule(BaseModel):
    """自定义 UTC+8 固定时间调度，可选择每日或指定市场交易日。"""

    id: str
    kind: Literal["fixed_time"] = "fixed_time"
    enabled: bool = True
    time: str
    calendar: CalendarCode = "daily"

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        """校验自定义调度 ID 为 UUID。

        参数：
            value: str，自定义调度 ID

        返回：
            str：通过校验的原值

        异常：
            ValueError，ID 不是合法 UUID 时抛出
        """
        try:
            normalized = str(UUID(value))
        except ValueError as exc:
            raise ValueError("自定义调度 id 必须为合法 UUID") from exc
        return normalized

    @field_validator("time")
    @classmethod
    def _check_time(cls, value: str) -> str:
        """校验 UTC+8 执行时间为严格 HH:MM。

        参数：
            value: str，待校验执行时间

        返回：
            str：通过校验的 HH:MM

        异常：
            ValueError，时间格式或范围非法时抛出
        """
        parts = value.split(":")
        if len(parts) != 2 or any(len(part) != 2 or not part.isdigit() for part in parts):
            raise ValueError("自定义调度 time 必须为 HH:MM")
        if not 0 <= int(parts[0]) <= 23 or not 0 <= int(parts[1]) <= 59:
            raise ValueError("自定义调度 time 超出合法范围")
        return value


ResearchSchedule = Annotated[MarketOpenSchedule | FixedTimeSchedule, Field(discriminator="kind")]


def _default_research_schedules() -> list[ResearchSchedule]:
    """创建三个不可删除的市场开盘预设。

    参数：无

    返回：
        list[ResearchSchedule]：东京、伦敦、纽约三个默认开启的开盘前调度
    """
    return [
        MarketOpenSchedule(id="asia_open", market="XTKS"),
        MarketOpenSchedule(id="europe_open", market="XLON"),
        MarketOpenSchedule(id="us_open", market="XNYS"),
    ]


class ResearchConfig(BaseModel):
    """研报 agent：数据源、循环参数、可配置自动调度与方向闸门。

    密钥不在此配置（只存 .env）：JIN10_MCP_TOKEN / BLOCKBEATS_API_KEY / FRED_API_KEY。
    schedules 统一保存三市场预设与自定义 UTC+8 时间；旧 time_* 字段加载时被忽略，
    缺少 schedules 的旧配置自动采用新三市场预设，首次写回后完成幂等迁移；
    gate_enabled/gate_max_age_hours 为方向闸门硬约束：研报方向结论在有效期内约束交易方向。
    """

    enabled: bool = False
    max_turns: int = Field(default=30, ge=1, le=100)
    timeout_seconds: int = Field(default=900, ge=60, le=3600)
    jin10_mcp_url: str = "https://mcp.jin10.com/mcp"
    blockbeats_mcp_cmd: str = "npx -y blockbeats-mcp"
    fred_base_url: str = "https://api.stlouisfed.org/fred"
    polymarket_base_url: str = "https://gamma-api.polymarket.com"
    schedules: list[ResearchSchedule] = Field(default_factory=_default_research_schedules)
    # 方向闸门硬约束开关：研报结论（多/空/中性）在 gate_max_age_hours 小时内强制生效
    gate_enabled: bool = True
    gate_max_age_hours: int = Field(default=13, ge=1, le=48)

    @model_validator(mode="after")
    def _check_schedules(self) -> ResearchConfig:
        """校验三预设完整、自定义 ID 唯一且启用时间不冲突。

        参数：无

        返回：
            ResearchConfig：通过调度结构与冲突校验的研报配置

        异常：
            ValueError，预设缺失、ID 重复或启用时间冲突时抛出
        """
        ids = [item.id for item in self.schedules]
        if len(ids) != len(set(ids)):
            raise ValueError("研报调度 id 不能重复")
        presets = {item.id for item in self.schedules if item.kind == "market_open"}
        if presets != {"asia_open", "europe_open", "us_open"}:
            raise ValueError("研报调度必须完整保留亚盘、欧盘、美盘三个预设")

        enabled_custom = [
            item for item in self.schedules if item.kind == "fixed_time" and item.enabled
        ]
        times = [item.time for item in enabled_custom]
        if len(times) != len(set(times)):
            raise ValueError("启用的自定义研报时间不能重复")
        possible = {
            "asia_open": {"07:30"},
            "europe_open": {"14:30", "15:30"},
            "us_open": {"21:00", "22:00"},
        }
        blocked = set().union(
            *(
                possible[item.id]
                for item in self.schedules
                if item.kind == "market_open" and item.enabled
            )
        )
        collision = sorted(set(times) & blocked)
        if collision:
            raise ValueError(f"自定义研报时间与市场预设冲突：{', '.join(collision)}")
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
    research: ResearchConfig = ResearchConfig()
    log: LogConfig = LogConfig()
    agents: AgentsConfig = AgentsConfig()

    @model_validator(mode="after")
    def _check_agent_credentials(self) -> Settings:
        """agent 引用的凭证必须存在（凭证重名也在此经 resolve_credentials 拦截）。

        researcher（研报 agent）为例外：可选功能（默认关闭），凭证缺失不阻塞
        配置加载——运行时研报会提示 LLM 未配置/不可用。

        参数：无

        返回：
            Settings，agent 引用的凭证必须存在（凭证重名也在此经 resolve_credentials 拦截）。  researcher（研报 agent）为例外：可选功能（默认关闭），凭证缺失不阻塞 配置加载——运行时研报会提示 LLM 未配置/不可用

        异常：
            ValueError，任一 Agent 引用了不存在或重名的凭证时抛出

        """
        names = {c.name for c in self.llm.resolve_credentials()}
        for agent, binding in self.agents.model_dump().items():
            if binding["credential"] not in names:
                if agent == "researcher":
                    continue  # 研报 agent 可选：凭证缺失降级为"研报不可用"
                raise ValueError(
                    f"agents.{agent}.credential 引用了不存在的凭证: {binding['credential']}"
                )
        return self

    def validate_mode(self) -> None:
        """校验运行模式 mode 是否为合法取值。

        参数：无

        返回：None，校验通过即正常返回，无副作用

        异常：
            ValueError：mode 不在 paper/testnet/live 之内时抛出
        """
        if self.mode not in ("paper", "testnet", "live"):
            raise ValueError(f"非法 mode: {self.mode}（可选 paper/testnet/live）")


class Watchlist(BaseModel):
    settle: str = "usdt"
    contracts: list[str]

    @model_validator(mode="after")
    def validate_contracts(self) -> "Watchlist":
        """校验关注列表合约非空且不重复。

        参数：无

        返回：
            Watchlist：校验通过后的当前模型实例

        异常：
            ValueError：contracts 为空列表，或同一合约被重复配置时抛出
        """
        if not self.contracts:
            raise ValueError("watchlist.contracts 不能为空，至少包含一个合约")
        if len(self.contracts) != len(set(self.contracts)):
            raise ValueError("watchlist.contracts 不能重复，同一合约只能配置一次")
        return self


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
        """校验指标短名单形状：去重保序后须 1~8 个，键只允许小写字母/数字/下划线。

        参数：无

        返回：
            IndicatorConfig：校验通过、shortlist 已替换为去重后列表的当前实例

        异常：
            ValueError：去重后数量不在 1~8 个区间，或存在含非法字符的指标键时抛出
        """
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
    """读取 YAML 文件为字典；文件不存在或内容为空时返回空字典。

    参数：
        path: Path，YAML 文件路径

    返回：
        dict：解析后的配置字典；文件缺失或内容为空时返回 {}
    """
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_settings(config_path: Path | None = None) -> Settings:
    """加载并校验主配置（默认 config.yaml），含运行模式合法性检查。

    参数：
        config_path: Path | None，配置文件路径；省略时读取项目根目录的 config.yaml

    返回：
        Settings：字段与运行模式均校验通过的完整配置模型
    """
    settings = Settings(**_load_yaml(config_path or ROOT / "config.yaml"))
    settings.validate_mode()
    return settings


def load_watchlist(path: Path | None = None) -> Watchlist:
    """加载交易合约关注列表（默认 watchlist.yaml）。

    参数：
        path: Path | None，配置文件路径；省略时读取项目根目录的 watchlist.yaml

    返回：
        Watchlist：合约非空且不重复校验通过后的关注列表模型
    """
    return Watchlist(**_load_yaml(path or ROOT / "watchlist.yaml"))


def load_indicator_config(path: Path | None = None) -> IndicatorConfig:
    """加载指标短名单；文件不存在返回默认基线（首次运行零配置可用）。

    参数：
        path: Path | None，指标配置文件路径

    返回：
        IndicatorConfig，加载指标短名单；文件不存在返回默认基线（首次运行零配置可用）
    """
    target = path or ROOT / "indicator_config.yaml"
    if not target.exists():
        return IndicatorConfig(shortlist=list(DEFAULT_INDICATOR_SHORTLIST))
    return IndicatorConfig(**_load_yaml(target))
