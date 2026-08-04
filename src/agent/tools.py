"""工具注册表：把 JSON schema（供 LLM）与异步执行函数绑定为 ToolSpec。

execute 是决策循环的唯一入口：任何工具失败（参数错误/交易所错误/内部异常）
都转成错误文本返回给 LLM，绝不向上抛异常中断本轮。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

from src.agent import tool_handlers, tool_plans, tool_trading
from src.agent.tool_handlers import ToolArgError, ToolDeps, ToolOutcome
from src.agent.tool_schemas import SCHEMAS
from src.audit.logger import get_logger
from src.gateway.base import GatewayError

logger = get_logger(__name__)

ToolHandler = Callable[[dict], Awaitable[ToolOutcome]]

# 工具名 → 执行函数（schema 在 tool_schemas.SCHEMAS 中同名定义；
# 交易类工具在 tool_trading，其余在 tool_handlers）
_HANDLERS: dict[str, Callable[[ToolDeps, dict], Awaitable[ToolOutcome]]] = {
    "get_market_data": tool_handlers.get_market_data,
    "get_indicators": tool_handlers.get_indicators,
    "place_order": tool_trading.place_order,
    "update_tpsl": tool_trading.update_tpsl,
    "amend_order": tool_trading.amend_order,
    "cancel_order": tool_trading.cancel_order,
    "set_price_alert": tool_handlers.set_price_alert,
    "cancel_price_alert": tool_handlers.cancel_price_alert,
    "set_next_wakeup": tool_handlers.set_next_wakeup,
    "write_note": tool_handlers.write_note,
    "get_history": tool_handlers.get_history,
    "calc": tool_handlers.calc,
    "update_trade_plan": tool_plans.update_trade_plan,
    "clear_trade_plan": tool_plans.clear_trade_plan,
}


@dataclass
class ToolSpec:
    """一个工具的完整定义：中性格式 schema + 异步执行函数。"""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema
    handler: ToolHandler


class ToolRegistry:
    """工具注册表：schemas() 供 provider 转换，execute() 统一捕获错误。"""

    def __init__(self, deps: ToolDeps) -> None:
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

    async def execute(self, name: str, args: dict | None) -> ToolOutcome:
        """执行工具；失败返回错误文本而非抛异常（LLM 可据此自我修正）。"""
        spec = self._tools.get(name)
        if spec is None:
            return ToolOutcome(f"错误：未知工具 {name}（可用：{', '.join(self._tools)}）")
        if args is not None and not isinstance(args, dict):
            return ToolOutcome("参数错误：工具参数必须是对象")
        try:
            return await spec.handler(args or {})
        except ToolArgError as e:
            return ToolOutcome(f"参数错误：{e}")
        except GatewayError as e:
            return ToolOutcome(f"交易所错误：{e}")
        except Exception as e:  # 工具内部 bug 不拖垮本轮，落日志排查
            logger.exception("工具 %s 执行异常", name)
            return ToolOutcome(f"工具内部错误：{type(e).__name__}: {e}")
