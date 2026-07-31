"""交易计划工具（update_trade_plan / clear_trade_plan）。

定位：纯记录类工具——维护全局唯一一份自由文本计划书（Markdown 全文覆盖更新），
不下单、不产生敞口，故不经 RiskEngine（真正下单仍走 tool_trading 的风控路径）；
调用照常进入统一审计（registry.execute → audit.record_tool_call），
清空原因经工具参数随审计留痕。
从 tool_handlers 拆出以控制单文件行数；校验辅助与 ToolDeps 经 tool_handlers 共享。
"""

from __future__ import annotations

from src.agent.tool_handlers import ToolArgError, ToolDeps, ToolOutcome, _need_str
from src.memory.plans_repo import MAX_PLAN_CHARS


async def update_trade_plan(deps: ToolDeps, args: dict) -> ToolOutcome:
    """全文覆盖更新交易计划（全局唯一一份，多合约想法写在同一份里）。"""
    content = _need_str(args, "content")
    if len(content) > MAX_PLAN_CHARS:
        raise ToolArgError(f"计划全文过长（{len(content)} 字符，上限 {MAX_PLAN_CHARS}）")
    plan = await deps.repo.plans.save_plan(deps.round_id, content)
    _notify_plan_updated(deps)
    return ToolOutcome(
        f"交易计划已更新（全文覆盖，{len(plan.content)} 字符）；"
        "下轮唤醒会在上下文「交易计划」小节看到本内容"
    )


async def clear_trade_plan(deps: ToolDeps, args: dict) -> ToolOutcome:
    """清空交易计划（计划已完成或作废时）；原因随审计留痕。"""
    _need_str(args, "reason")  # 强制写明原因（进审计），内容本身无需落库
    if await deps.repo.plans.get_plan() is None:
        return ToolOutcome("当前本就没有交易计划，无需清空")
    await deps.repo.plans.clear_plan(deps.round_id)
    _notify_plan_updated(deps)
    return ToolOutcome("交易计划已清空；如有新的条件性意图请用 update_trade_plan 重新立案")


def _notify_plan_updated(deps: ToolDeps) -> None:
    """计划变更即广播 WS 事件（前端据此立即重拉面板，不等轮末）；未接线时静默跳过。"""
    if deps.notify_event is not None:
        deps.notify_event({"type": "plan_updated"})
