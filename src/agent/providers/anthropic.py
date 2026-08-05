"""Anthropic Claude 实现：官方 AsyncAnthropic SDK，key 从 ANTHROPIC_API_KEY 读取。

格式要点（官方 SDK）：
- 工具定义 {name, description, input_schema}；响应 content 为 block 列表，
  text block 为文本，tool_use block（id/name/input）为工具调用
- 工具结果以 user 角色的 tool_result block 回填，tool_use_id 对应调用 ID
"""

from __future__ import annotations

import os
from typing import Any

import anthropic

from src.agent.providers.base import LLMError, LLMResponse, ToolCall
from src.agent.providers.thinking import anthropic_thinking
from src.config import CredentialConfig, LLMConfig


class AnthropicProvider:
    """Anthropic Messages API 适配。"""

    def __init__(self, config: LLMConfig | CredentialConfig, api_key: str | None = None) -> None:
        key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise LLMError("缺少 ANTHROPIC_API_KEY 环境变量，无法初始化 Anthropic provider")
        self._client = anthropic.AsyncAnthropic(api_key=key)
        self._model = config.model
        self._max_tokens = config.max_tokens
        self._thinking_effort = config.thinking_effort

    async def chat(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        req: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": system,
            "messages": messages,
        }
        thinking = anthropic_thinking(self._thinking_effort, self._max_tokens)
        if thinking is not None:
            req["thinking"] = thinking
        if tools:
            req["tools"] = [
                {
                    "name": t["name"],
                    "description": t["description"],
                    "input_schema": t["parameters"],
                }
                for t in tools
            ]
        try:
            resp = await self._client.messages.create(**req)
        except anthropic.APIError as e:
            raise LLMError(f"Anthropic API 错误: {e}") from e
        return self._parse(resp)

    def tool_result_message(self, call: ToolCall, result: str) -> dict:
        return {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": call.call_id, "content": result}],
        }

    @staticmethod
    def _parse(resp: anthropic.types.Message) -> LLMResponse:
        """content block 列表 → 统一 LLMResponse；SDK 已把 tool_use.input 解析为 dict。"""
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                args = block.input if isinstance(block.input, dict) else {}
                calls.append(ToolCall(name=block.name, args=args, call_id=block.id))
        return LLMResponse(
            text="".join(text_parts),
            tool_calls=calls,
            raw=resp.model_dump_json(),
            assistant_message={
                "role": "assistant",
                "content": [b.model_dump() for b in resp.content],
            },
        )
