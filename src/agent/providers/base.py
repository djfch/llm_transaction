"""LLM Provider 抽象：统一不同厂商的聊天/工具调用接口。

各厂商 SDK 的消息/工具格式差异由各自 provider 内部消化：
- chat 返回统一 LLMResponse（文本 + tool_calls + raw 原文 + 原生 assistant 消息）
- tool_result_message 把工具执行结果包装成厂商原生消息，供多轮对话回填
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class LLMError(Exception):
    """LLM 调用失败（网络/鉴权/限流/服务端错误等）。"""


class LLMParseError(LLMError):
    """LLM 输出解析失败（如工具参数不是合法 JSON）：本轮不得交易。"""


@dataclass
class ToolCall:
    """一次工具调用请求（LLM 输出解析后的统一形式）。"""

    name: str
    args: dict[str, Any]
    call_id: str = ""  # 厂商侧调用 ID（Anthropic tool_use_id / OpenAI tool_call_id）


@dataclass
class LLMResponse:
    """一次 LLM 回复。"""

    text: str = ""  # 文本部分（可能为空）
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: str = ""  # 原始输出全文（审计快照用）
    assistant_message: dict[str, Any] | None = None  # 原生 assistant 消息（多轮回填用）


class LLMProvider(Protocol):
    """LLM 供应商统一接口。"""

    async def chat(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        """发起一轮对话。tools 为中性格式 {name, description, parameters(JSON Schema)}。

        参数：
            system: str，系统提示词
            messages: list[dict]，本轮对话消息列表
            tools: list[dict]，中性格式的工具定义列表

        返回：
            LLMResponse：发起一轮对话。tools 为中性格式 {name, description, parameters(JSON Schema)}
        """
        ...

    def tool_result_message(self, call: ToolCall, result: str) -> dict:
        """把一次工具执行结果包装成厂商原生消息，追加进 messages 继续对话。

        参数：
            call: ToolCall，已执行的工具调用
            result: str，待序列化或返回的执行结果

        返回：
            dict：把一次工具执行结果包装成厂商原生消息，追加进 messages 继续对话
        """
        ...
