"""交易计划工具（save_trade_plan / close_trade_plan）。

定位：纯记录类工具——把 agent 的条件性交易意图落成结构化挂起计划，
不下单、不产生敞口，故不经 RiskEngine（真正下单仍走 tool_trading 的风控路径）；
调用照常进入统一审计（registry.execute → audit.record_tool_call）。
从 tool_handlers 拆出以控制单文件行数；校验辅助与 ToolDeps 经 tool_handlers 共享。
"""

from __future__ import annotations

import time

from src.agent.tool_handlers import (
    ToolArgError,
    ToolDeps,
    ToolOutcome,
    _need_enum,
    _need_str,
    _opt_int,
)

_MAX_VALID_HOURS = 720  # 计划有效期上限（30 天），防止无限期僵尸计划

_DIRECTION_TEXT = {"long": "做多", "short": "做空"}
_OUTCOME_TEXT = {"executed": "已执行", "cancelled": "已放弃"}


def _opt_str(args: dict, name: str) -> str:
    """可选字符串参数：缺省返回空串；给了就必须是字符串。"""
    v = args.get(name)
    if v is None:
        return ""
    if not isinstance(v, str):
        raise ToolArgError(f"参数 {name} 必须是字符串")
    return v.strip()


def _expires_at(args: dict) -> float | None:
    """valid_hours（可选，1-720 小时）→ 绝对过期时间戳；缺省不设有效期。"""
    hours = _opt_int(args, "valid_hours", 0)
    if hours == 0:
        return None
    if not 1 <= hours <= _MAX_VALID_HOURS:
        raise ToolArgError(f"valid_hours 必须在 1-{_MAX_VALID_HOURS} 小时之间")
    return time.time() + hours * 3600


async def save_trade_plan(deps: ToolDeps, args: dict) -> ToolOutcome:
    """立/换交易计划：同合约旧 active 计划自动作废（被新计划替代）。"""
    contract = _need_str(args, "contract")
    direction = _need_enum(args, "direction", {"long", "short"})
    plan = await deps.repo.plans.save_plan(
        round_id=deps.round_id,
        contract=contract,
        direction=direction,
        entry=_need_str(args, "entry"),
        stop_loss=_need_str(args, "stop_loss"),
        take_profit=_need_str(args, "take_profit"),
        condition=_need_str(args, "condition"),
        size_hint=_opt_str(args, "size_hint"),
        rationale=_opt_str(args, "rationale"),
        expires_at=_expires_at(args),
    )
    return ToolOutcome(
        f"交易计划已保存（plan_id={plan.id}，{contract} {_DIRECTION_TEXT[direction]}）；"
        "该合约旧 active 计划（若有）已自动作废。执行或放弃后请用 close_trade_plan 收尾"
    )


async def close_trade_plan(deps: ToolDeps, args: dict) -> ToolOutcome:
    """收尾交易计划：executed（已按计划执行）或 cancelled（放弃），必须写明原因。"""
    plan_id = _opt_int(args, "plan_id", 0)
    if plan_id <= 0:
        raise ToolArgError("缺少必填参数 plan_id（正整数）")
    outcome = _need_enum(args, "outcome", {"executed", "cancelled"})
    reason = _need_str(args, "reason")
    plan = await deps.repo.plans.close_plan(plan_id, outcome, reason)
    if plan is None:
        return ToolOutcome(
            f"未找到 active 状态的计划 plan_id={plan_id}（不存在或已收尾）；"
            "可查看上下文「交易计划」小节确认当前计划"
        )
    return ToolOutcome(f"计划 plan_id={plan.id}（{plan.contract}）已标记为{_OUTCOME_TEXT[outcome]}")
