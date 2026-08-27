"""复盘工具注册表：12 个工具的 JSON schema（供 LLM）与异步执行函数绑定。

安全不变量：本注册表无任何交易工具、不持有 Gateway 引用；
execute 是复盘 agent 的唯一执行入口，任何失败（未知工具/参数错误/内部异常）
都转成中文错误文本返回给 LLM，绝不向上抛异常中断本轮。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

from src.audit.logger import get_logger
from src.review import tool_handlers, tool_indicators, tool_research, tool_research_prompt
from src.review.tool_handlers import ReviewToolDeps, ToolArgError
from src.review.tool_schemas import SCHEMAS

logger = get_logger(__name__)

# 工具名 → 执行函数（schema 在 tool_schemas.SCHEMAS 中同名定义）
_HANDLERS: dict[str, Callable[[ReviewToolDeps, dict], Awaitable[str]]] = {
    "get_review_stats": tool_handlers.get_review_stats,
    "list_decision_rounds": tool_handlers.list_decision_rounds,
    "get_decision_detail": tool_handlers.get_decision_detail,
    "get_tool_call_chain": tool_handlers.get_tool_call_chain,
    "list_trades": tool_handlers.list_trades,
    "get_round_context": tool_handlers.get_round_context,
    "get_strategy_versions": tool_handlers.get_strategy_versions,
    "calc": tool_handlers.calc,
    "submit_strategy_revision": tool_handlers.submit_strategy_revision,
    "get_indicators": tool_indicators.get_indicators,
    "get_indicator_config": tool_indicators.get_indicator_config,
    "submit_indicator_config": tool_indicators.submit_indicator_config,
    "list_research_review_candidates": tool_research.list_research_review_candidates,
    "get_research_review_case": tool_research.get_research_review_case,
    "list_research_reviews": tool_research.list_research_reviews,
    "submit_research_review": tool_research.submit_research_review,
    "get_research_prompt_versions": tool_research_prompt.get_research_prompt_versions,
    "submit_research_prompt_revision": tool_research_prompt.submit_research_prompt_revision,
}


@dataclass
class ToolSpec:
    """一个复盘工具的完整定义：中性格式 schema + 异步执行函数。"""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema
    handler: Callable[[dict], Awaitable[str]]


class ReviewToolRegistry:
    """复盘工具注册表：schemas() 供 provider 转换，execute() 统一捕获错误。"""

    def __init__(self, deps: ReviewToolDeps) -> None:
        """构建注册表：把每个工具的处理函数与同名 schema 组装成 ToolSpec。

        参数：
            deps: ReviewToolDeps，复盘工具执行所需依赖，经 partial 预绑定到每个处理函数

        返回：
            None，就地填充实例的 _tools 工具字典
        """
        self._tools: dict[str, ToolSpec] = {}
        for name, fn in _HANDLERS.items():
            schema = SCHEMAS[name]
            self._tools[name] = ToolSpec(
                name=name,
                description=schema["description"],
                parameters=schema["parameters"],
                handler=partial(fn, deps),
            )

    @property
    def specs(self) -> list[ToolSpec]:
        """列出全部已注册工具的完整定义。

        参数：无

        返回：
            list[ToolSpec]：每个工具的中性 schema 与异步执行函数
        """
        return list(self._tools.values())

    def schemas(self) -> list[dict]:
        """中性格式 [{name, description, parameters}]，provider 各自转换。

        参数：无
        返回：
            list[dict]，中性格式 [{name, description, parameters}]，provider 各自转换
        """
        return [
            {"name": s.name, "description": s.description, "parameters": s.parameters}
            for s in self._tools.values()
        ]

    async def execute(self, name: str, args: dict | None) -> str:
        """执行工具；失败返回错误文本而非抛异常（LLM 可据此自我修正）。

        参数：
            name: str，工具、凭证或对象名称
            args: dict | None，工具调用参数
        返回：
            str，执行工具；失败返回错误文本而非抛异常（LLM 可据此自我修正）
        """
        spec = self._tools.get(name)
        if spec is None:
            return f"错误：未知工具 {name}（可用：{', '.join(self._tools)}）"
        if args is not None and not isinstance(args, dict):
            return "参数错误：工具参数必须是对象"
        try:
            return await spec.handler(args or {})
        except ToolArgError as e:
            return f"参数错误：{e}"
        except Exception as e:  # 工具内部 bug 不拖垮本轮复盘，落日志排查
            logger.exception("复盘工具 %s 执行异常", name)
            return f"工具内部错误：{type(e).__name__}: {e}"
