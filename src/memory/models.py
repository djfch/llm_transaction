"""持久化层的记录模型（与表结构一一对应）。

金额/数量字段为 Decimal（落库时是 TEXT，pydantic 读取时自动从字符串还原）。
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel


class Decision(BaseModel):
    """一轮 LLM 决策记录。"""

    id: int
    round_id: str
    mode: str
    strategy_version: str
    # 策略书原文 md5（与 strategy_versions.md5 关联）；strategy_version 为
    # 「策略书+工具说明段」拼装 md5，两者并存、语义不同
    strategy_md5: str = ""
    wake_source: str
    context_summary: str
    llm_raw: str
    created_at: float


class OrderRecord(BaseModel):
    """订单记录。price 为 None 表示市价单。"""

    id: str
    round_id: str
    mode: str
    contract: str
    side_size: Decimal
    price: Decimal | None
    tif: str
    text: str
    status: str
    finish_as: str
    created_at: float


class Trade(BaseModel):
    """成交记录。size 正多负空；pnl 为已实现盈亏。

    source 成交来源（全项目统一枚举）：
    - 'llm_open': LLM 开仓；'llm_close': LLM 决策平仓；'user_close': 用户手动平仓
    - 'liquidation': 强平；'tpsl_close': 止盈止损保护触发；'': 历史/未知
    testnet/live 成交经 Gate 私有推送 + 事件驱动对账同步（fill_sync），exchange_trade_id
    列仅后端去重用，不进本模型、不进 /api/trades 响应。
    """

    id: int
    round_id: str
    mode: str
    contract: str
    size: Decimal
    price: Decimal
    fee: Decimal
    pnl: Decimal
    source: str
    created_at: float


class Note(BaseModel):
    """Agent 自述笔记（跨轮传递上下文）。"""

    id: int
    round_id: str
    content: str
    created_at: float


class AuditRound(BaseModel):
    """审计：一轮决策全过程。ended_at 为 None 表示该轮未结束。"""

    round_id: str
    mode: str
    wake_source: str
    prompt_md5: str
    strategy_md5: str = ""  # 策略书原文 md5，语义同 Decision.strategy_md5
    prompt_snapshot: str
    context_snapshot: str
    llm_raw: str
    started_at: float
    ended_at: float | None
    error: str


class AuditToolCall(BaseModel):
    """审计：一轮中的一次工具调用（含风控判定与耗时）。"""

    id: int
    round_id: str
    seq: int
    tool: str
    args_json: str
    risk_verdict: str
    risk_reason: str
    result_json: str
    duration_ms: int
    created_at: float


class StrategyVersion(BaseModel):
    """策略书版本：content 为完整原文，md5 与 decisions/audit_rounds.strategy_md5 关联。

    created_by 取值：human（人工修改）/ review_agent（复盘 agent 改写）/ rollback（回滚）。
    report_id 指向触发本版本的复盘报告（人工版本为 None）。
    """

    id: int
    content: str
    md5: str
    created_by: str
    reason: str
    report_id: int | None = None
    created_at: float


class IndicatorConfigVersion(BaseModel):
    """指标短名单版本：content 为 indicator_config.yaml 完整原文，md5 与原文内容关联。

    created_by 取值：human（人工修改/初始播种）/ review_agent（复盘 agent 改写）/ rollback（回滚）。
    report_id 指向触发本版本的复盘报告（人工版本为 None）。
    """

    id: int
    content: str
    md5: str
    created_by: str
    reason: str
    report_id: int | None = None
    created_at: float


class ReviewReport(BaseModel):
    """复盘报告：period 为统计区间（Unix 秒），stats_json 为代码侧统计结果。

    strategy_action 取值：none（无需调整）/ rewrite（产出新策略版本，new_version_id 指向它）。
    error 非空表示该次复盘失败（只落错误记录，不影响交易循环）。
    round_id 为产生本报告的审计轮 id；空串 = 无关联（功能上线前的老报告，不回填）。
    """

    id: int
    period_start: float
    period_end: float
    stats_json: str
    report_md: str
    strategy_action: str
    new_version_id: int | None = None
    error: str = ""
    round_id: str = ""
    created_at: float


class TradePlan(BaseModel):
    """交易计划：全局唯一一份的自由文本计划书（建议性，不自动下单、不经风控）。

    content 为 Markdown 全文（多合约想法写在同一份里），更新即全文覆盖；
    空串表示当前无计划。历史不单独留表——每轮审计上下文快照已冻结当轮计划原文。
    """

    round_id: str = ""
    content: str = ""
    updated_at: float


class Timeline(BaseModel):
    """事实层记录（研报系统）：代码增量写入的事件流，LLM 零写权限。

    source 取值：jin10 / blockbeats（数据来源）；
    kind 取值：flash（快讯）/ calendar（日历事件）/ indicator（指标快照）；
    meta_json 存结构化附加（日历事件带 actual/consensus/previous/star，指标带数值）。
    dedup_key 为「来源+时间+标题」哈希，唯一约束保证增量幂等。
    """

    id: int
    source: str
    kind: str
    title: str
    url: str = ""
    published_at: float
    meta_json: str = "{}"
    dedup_key: str
    fetched_at: float


class ResearchReport(BaseModel):
    """研报报告头：逐标的结论统一存于 ResearchAssetView。

    report_type 取值：manual（手动触发）/ asia_open / europe_open / us_open（三盘定时 slot）；
    schema_version 固定为 2；error 非空表示本次研报失败且没有逐标的结论；
    round_id 为产生本研报的审计轮 id。
    """

    id: int
    report_type: str
    schema_version: int = 2
    summary: str = ""
    cross_market_view: str = ""
    global_risks_json: str = "[]"
    raw_json: str = "{}"
    error: str = ""
    round_id: str = ""
    created_at: float


class ResearchAssetView(BaseModel):
    """研报 v2 的单合约结论及当时市场输入快照。"""

    id: int
    report_id: int
    contract: str
    direction: str
    confidence: str
    horizon: str = ""
    market_regime: str = ""
    technical_confirmation: str = ""
    basis_type: str = ""
    data_status: str = ""
    evidence_json: str = "[]"
    risks_json: str = "[]"
    narrative: str = ""
    market_context_json: str = "{}"
    verify_result: str = ""
    created_at: float


class CausalLink(BaseModel):
    """因果链（分析笔记）：研报 agent 提交的链式因果推导，复盘验证状态。

    chain_json 为有序节点链 JSON（node/kind/timeline_id）；
    status 取值：pending（待验证）/ verified（复盘确认）/ failed（复盘否决）/
    superseded（已被更新版替代）；
    broken_at 为断点节点下标（复盘标记，None = 未定位）；
    topic 为事件主题（同主题多次提交聚合成族）；
    supersedes_id 为新链声明的替代目标（版本化：旧链保留留档）；
    await_verification 为 agent 显式声明（True=待验证中间态，允许半成品，
    进入未闭合监控池；False=结论链）。
    """

    id: int
    report_id: int
    chain_json: str
    confidence: float
    evidence_json: str = "[]"
    status: str = "pending"
    broken_at: int | None = None
    topic: str = ""
    supersedes_id: int | None = None
    await_verification: bool = True
    created_at: float
