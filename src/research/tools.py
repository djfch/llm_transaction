"""研报工具注册表：11 个工具的 JSON schema（供 LLM）与异步执行函数绑定。

安全不变量：本注册表无任何交易工具、不持有 Gateway 引用；
execute 是研报 agent 的唯一执行入口，任何失败（未知工具/参数错误/内部异常）
都转成中文错误文本返回给 LLM，绝不向上抛异常中断本轮。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

from src.audit.logger import get_logger
from src.research import tool_handlers
from src.research.tool_handlers import ResearchToolDeps, ToolArgError
from src.research.tool_schemas import SCHEMAS

logger = get_logger(__name__)

# 工具名 → 执行函数（schema 在 tool_schemas.SCHEMAS 中同名定义）
_HANDLERS: dict[str, Callable[[ResearchToolDeps, dict], Awaitable[str]]] = {
    "get_research_market_data": tool_handlers.get_research_market_data,
    "fetch_calendar": tool_handlers.fetch_calendar,
    "fetch_flash": tool_handlers.fetch_flash,
    "fetch_indicators": tool_handlers.fetch_indicators,
    "get_macro_series": tool_handlers.get_macro_series,
    "get_prediction_markets": tool_handlers.get_prediction_markets,
    "fetch_article_detail": tool_handlers.fetch_article_detail,
    "search_news": tool_handlers.search_news,
    "read_timeline": tool_handlers.read_timeline,
    "read_judgments": tool_handlers.read_judgments,
    "read_causal_links": tool_handlers.read_causal_links,
    "submit_causal_links": tool_handlers.submit_causal_links,
}


@dataclass
class ToolSpec:
    """一个研报工具的完整定义：中性格式 schema + 异步执行函数。"""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema
    handler: Callable[[dict], Awaitable[str]]


class ResearchToolRegistry:
    """研报工具注册表：schemas() 供 provider 转换，execute() 统一捕获错误。"""

    def __init__(self, deps: ResearchToolDeps) -> None:
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
        return list(self._tools.values())

    def schemas(self) -> list[dict]:
        """中性格式 [{name, description, parameters}]，provider 各自转换。"""
        return [
            {"name": s.name, "description": s.description, "parameters": s.parameters}
            for s in self._tools.values()
        ]

    async def execute(self, name: str, args: dict | None) -> str:
        """执行工具；失败返回错误文本而非抛异常（LLM 可据此自我修正）。"""
        spec = self._tools.get(name)
        if spec is None:
            return f"错误：未知工具 {name}（可用：{', '.join(self._tools)}）"
        if args is not None and not isinstance(args, dict):
            return "参数错误：工具参数必须是对象"
        try:
            return await spec.handler(args or {})
        except ToolArgError as e:
            return f"参数错误：{e}"
        except Exception as e:  # 工具内部 bug 不拖垮本轮研报，落日志排查
            logger.exception("研报工具 %s 执行异常", name)
            return f"工具内部错误：{type(e).__name__}: {e}"
