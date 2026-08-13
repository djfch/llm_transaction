"""OpenAI 兼容实现：官方 AsyncOpenAI SDK，key 从 OPENAI_API_KEY 读取。

base_url 可配（LLMConfig.openai_base_url），兼容国产模型的 OpenAI 兼容端点
（DeepSeek、通义、Kimi 等）。工具格式 {"type":"function","function":{...}}；
工具参数为 JSON 字符串，解析失败抛 LLMParseError（本轮不交易）。
"""

from __future__ import annotations

import json
import os

import openai

from src.agent.providers.base import LLMError, LLMParseError, LLMResponse, ToolCall
from src.agent.providers.thinking import thinking_wire_kwargs
from src.config import CredentialConfig, LLMConfig


class OpenAICompatProvider:
    """OpenAI chat.completions 兼容协议适配。"""

    def __init__(self, config: LLMConfig | CredentialConfig, api_key: str | None = None) -> None:
        """初始化 OpenAI 兼容客户端：取 key、建 AsyncOpenAI 并记下模型参数。

        参数：
            config: LLMConfig | CredentialConfig，LLM 配置（模型名、max_tokens、
                thinking_effort、可选的 openai_base_url）
            api_key: str | None，显式传入的 API key；为 None 时从环境变量
                OPENAI_API_KEY 读取

        返回：
            None，就地初始化实例属性（创建 self._client 异步客户端）

        异常：
            LLMError：未提供 api_key 且环境变量 OPENAI_API_KEY 也为空时抛出
        """
        key = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not key:
            raise LLMError("缺少 OPENAI_API_KEY 环境变量，无法初始化 OpenAI 兼容 provider")
        # 重试收口到 RetryingProvider 一层：SDK 默认 max_retries=2 会与之叠乘
        # （持久故障时 HTTP 请求数与延迟放大 3 倍，偏离"累计 3 次"口径）
        kwargs: dict = {"api_key": key, "max_retries": 0}
        if config.openai_base_url:
            kwargs["base_url"] = config.openai_base_url
        self._client = openai.AsyncOpenAI(**kwargs)
        self._model = config.model
        self._max_tokens = config.max_tokens
        self._thinking_effort = config.thinking_effort

    async def chat(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        """发起一轮对话：把中性工具定义转成 OpenAI 格式并调用 chat.completions。

        参数：
            system: str，系统提示词（作为首条 system 消息发送）
            messages: list[dict]，多轮对话消息（厂商原生格式的历史消息）
            tools: list[dict]，中性格式工具定义 {name, description, parameters(JSON Schema)}；
                空列表时按 None 传给 API（不启用工具）

        返回：
            LLMResponse：统一回复（文本 + 工具调用列表 + 原文 + 原生 assistant 消息）

        异常：
            LLMError：OpenAI 兼容 API 返回错误（网络/鉴权/限流/服务端等）时抛出
        """
        oai_tools = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters"],
                },
            }
            for t in tools
        ]
        thinking_kwargs = thinking_wire_kwargs(self._model, self._thinking_effort)
        try:
            resp = await self._client.chat.completions.create(
                model=self._model,
                max_tokens=self._max_tokens,
                messages=[{"role": "system", "content": system}, *messages],
                tools=oai_tools or None,
                **thinking_kwargs,
            )
        except openai.APIError as e:
            raise LLMError(f"OpenAI 兼容 API 错误: {e}") from e
        raw = resp.model_dump_json()
        try:
            return self._parse(resp)
        except Exception as exc:
            setattr(exc, "raw", raw)
            raise

    def tool_result_message(self, call: ToolCall, result: str) -> dict:
        """把一次工具执行结果包装成 OpenAI 原生 tool 消息，供回填 messages 继续对话。

        参数：
            call: ToolCall，对应的工具调用（取其 call_id 关联回请求）
            result: str，工具执行结果的文本内容

        返回：
            dict：OpenAI 格式的 tool 角色消息
                {"role": "tool", "tool_call_id": ..., "content": ...}
        """
        return {"role": "tool", "tool_call_id": call.call_id, "content": result}

    @staticmethod
    def _parse(resp: openai.types.chat.ChatCompletion) -> LLMResponse:
        """把 OpenAI ChatCompletion 原始响应解析成统一 LLMResponse。

        参数：
            resp: openai.types.chat.ChatCompletion，OpenAI SDK 返回的原始响应对象

        返回：
            LLMResponse：统一回复（文本 + 解析后的工具调用列表 + 原文 JSON +
                原生 assistant 消息）

        异常：
            LLMParseError：响应不含任何 choice、工具参数不是合法 JSON、
                或工具参数解析结果不是 JSON 对象时抛出
        """
        if not resp.choices:
            raise LLMParseError("LLM 响应不含任何 choice")
        msg = resp.choices[0].message
        calls: list[ToolCall] = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments) if tc.function.arguments else {}
            except json.JSONDecodeError as e:
                raise LLMParseError(
                    f"工具 {tc.function.name} 参数不是合法 JSON: {tc.function.arguments[:200]}"
                ) from e
            if not isinstance(args, dict):
                raise LLMParseError(f"工具 {tc.function.name} 参数必须是 JSON 对象")
            calls.append(ToolCall(name=tc.function.name, args=args, call_id=tc.id))
        return LLMResponse(
            text=msg.content or "",
            tool_calls=calls,
            raw=resp.model_dump_json(),
            assistant_message=msg.model_dump(exclude_none=True),
        )
