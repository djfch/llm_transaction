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
    llm_credential_name: str = ""  # 本轮实际使用的 LLM 凭证名（空=未知/历史轮次）
    llm_provider: str = ""  # 本轮 LLM 厂商（anthropic/openai_compat/openai_responses）
    llm_model: str = ""  # 本轮实际调用的模型名
    llm_thinking_effort: str = ""  # 本轮思考强度档位（空=跟随模型默认/未知）


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
    status: str = "applied"  # applied 已生效 / draft 草稿 / discarded 已废弃（issue #62/#73）


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
    status: str = "applied"  # applied 已生效 / draft 草稿 / discarded 已废弃（issue #62/#73）


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
    schema_version 取值：2（历史代际）/ 3（当前代际，issue #113 起写入）；error 非空表示
    本次研报失败且没有逐标的结论；round_id 为产生本研报的审计轮 id；
    research_prompt_md5 为生成本研报所用的 research_prompt.md 正文 md5（与
    research_prompt_versions.md5 关联；空串 = 功能上线前的历史研报，不回填）。
    """

    id: int
    report_type: str
    schema_version: int = 3
    summary: str = ""
    cross_market_view: str = ""
    global_risks_json: str = "[]"
    raw_json: str = "{}"
    error: str = ""
    round_id: str = ""
    created_at: float
    research_prompt_md5: str = ""


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
    created_at: float


class CausalLink(BaseModel):
    """因果链（分析笔记）：研报 agent 提交的链式因果推导。

    chain_json 为有序节点链 JSON（node/kind/timeline_id）；
    status 取值：tracking（待跟踪，事件仍在发展）/ concluded（已结论）/
    superseded（已被更新版替代）；
    topic 为事件主题（同主题多次提交聚合成族）；
    supersedes_id 为新链声明的替代目标（版本化：旧链保留留档）。
    """

    id: int
    report_id: int
    chain_json: str
    confidence: float
    evidence_json: str = "[]"
    status: str = "tracking"
    topic: str = ""
    supersedes_id: int | None = None
    created_at: float


class ResearchReview(BaseModel):
    """研报复盘记录：复盘 agent 对一份研报中单个合约结论的批改（issue #113）。

    review_report_id 指向产生本记录的复盘报告；report_id+contract 定位被复盘的
    逐标的结论；同一份复盘报告内 (report_id, contract) 唯一，同一研报可被多次复盘。
    direction_relation（方向关系）/reasoning_quality（推理质量）/confidence_assessment
    （置信度合规）为枚举评价，对应 direction_reason/reasoning_review/confidence_reason
    为各枚举的评价理由文本；improvement_advice（改进建议）为自由文本；
    evidence_reviews_json 为逐条依据评价列表（与原研报 evidence 一一对应，后端强制
    1:1 校验，每条含 evidence_index/fact_status/reasoning_status/explanation）；
    outcome_json 为代码按历史 K 线计算的客观行情结果（LLM 不可写）。
    """

    id: int
    review_report_id: int
    report_id: int
    contract: str
    direction_relation: str = ""
    direction_reason: str = ""
    reasoning_quality: str = ""
    reasoning_review: str = ""
    evidence_reviews_json: str = "[]"
    confidence_assessment: str = ""
    confidence_reason: str = ""
    improvement_advice: str = ""
    outcome_json: str = "{}"
    created_at: float


class ResearchPromptVersion(BaseModel):
    """研报提示词版本：content 为 research_prompt.md 正文完整原文（issue #113）。

    md5 与 research_reports.research_prompt_md5 关联（研报落库时记录所用正文 md5）。
    created_by 取值：human（人工修改/初始播种）/ review_agent（复盘 agent 改写）/
    rollback（回滚）。review_report_id 指向触发本版本的复盘报告（人工版本为 None）。
    status 取值：applied 已生效 / draft 草稿 / discarded 已废弃。
    """

    id: int
    content: str
    md5: str
    created_by: str
    reason: str
    review_report_id: int | None = None
    created_at: float
    status: str = "applied"


class ResearchReviewCandidate(BaseModel):
    """研报复盘候选：一份研报中已到期且尚未被正式复盘的单条逐标的结论（issue #113）。

    由 research_review_repo.list_review_candidates 的联表查询构造（非数据表行）；
    due_at 为报告创建时间 + horizon 窗口秒数（到期时刻），按它升序返回。
    """

    report_id: int
    contract: str
    direction: str
    confidence: str
    horizon: str
    report_type: str
    report_created_at: float
    due_at: float
