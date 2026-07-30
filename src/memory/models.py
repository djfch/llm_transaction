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
    - 'liquidation': 强平；'tpsl_close': 止盈止损（一期预留）；'': 历史/未知
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


class ReviewReport(BaseModel):
    """复盘报告：period 为统计区间（Unix 秒），stats_json 为代码侧统计结果。

    strategy_action 取值：none（无需调整）/ rewrite（产出新策略版本，new_version_id 指向它）。
    error 非空表示该次复盘失败（只落错误记录，不影响交易循环）。
    """

    id: int
    period_start: float
    period_end: float
    stats_json: str
    report_md: str
    strategy_action: str
    new_version_id: int | None = None
    error: str = ""
    created_at: float


class TradePlan(BaseModel):
    """交易计划：agent 的挂起条件单记录（建议性，不自动下单、不经风控）。

    entry/stop_loss/take_profit 为自由文本（允许区间描述如 "64200-64300"）；
    direction 取值 long/short；status 取值 active/executed/cancelled；
    expires_at 为 None 表示不设有效期（过期不自动关闭，上下文标注提醒 agent 处理）。
    """

    id: int
    round_id: str
    contract: str
    direction: str
    entry: str
    stop_loss: str
    take_profit: str
    size_hint: str = ""
    condition: str
    rationale: str = ""
    expires_at: float | None = None
    status: str = "active"
    closed_reason: str = ""
    created_at: float
    updated_at: float
