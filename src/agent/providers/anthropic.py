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
        """初始化 Anthropic 异步客户端并保存模型配置。

        参数：
            config: LLMConfig | CredentialConfig，LLM 配置（读取模型名、最大输出 token 数、
                思考强度）
            api_key: str | None，Anthropic API 密钥；省略时从环境变量 ANTHROPIC_API_KEY 读取

        返回：
            None，创建 AsyncAnthropic 客户端并保存为实例属性

        异常：
            LLMError：参数与环境变量均未提供 API 密钥时抛出
        """
        key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise LLMError("缺少 ANTHROPIC_API_KEY 环境变量，无法初始化 Anthropic provider")
        # 重试收口到 RetryingProvider 一层：SDK 默认 max_retries=2 会与之叠乘
        # （持久故障时 HTTP 请求数与延迟放大 3 倍，偏离"累计 3 次"口径）
        self._client = anthropic.AsyncAnthropic(api_key=key, max_retries=0)
        self._model = config.model
        self._max_tokens = config.max_tokens
        self._thinking_effort = config.thinking_effort

    async def chat(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        """调用 Anthropic Messages API 发起一轮对话，返回统一格式的回复。

        参数：
            system: str，系统提示词
            messages: list[dict]，多轮对话消息列表（Anthropic 原生格式）
            tools: list[dict]，中性格式工具定义 {name, description, parameters(JSON Schema)}，
                内部转换为 Anthropic 的 input_schema 格式；空列表表示不带工具

        返回：
            LLMResponse：统一回复（文本 + 工具调用列表 + 原始输出 + 原生 assistant 消息）

        异常：
            LLMError：Anthropic API 调用失败（网络/鉴权/限流/服务端错误等）时抛出
        """
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
        """把一次工具执行结果包装成 Anthropic 原生 tool_result 消息，供回填继续对话。

        参数：
            call: ToolCall，对应的工具调用（取 call_id 作为 tool_use_id 关联原调用）
            result: str，工具执行结果文本

        返回：
            dict：role 为 user、content 为 tool_result block 的消息
        """
        return {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": call.call_id, "content": result}],
        }

    @staticmethod
    def _parse(resp: anthropic.types.Message) -> LLMResponse:
        """content block 列表 → 统一 LLMResponse；SDK 已把 tool_use.input 解析为 dict。

        参数：
            resp: anthropic.types.Message，提供商原始响应

        返回：
            LLMResponse，由文本块和工具调用块组成的统一 LLM 响应
        """
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
