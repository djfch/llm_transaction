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
        key = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not key:
            raise LLMError("缺少 OPENAI_API_KEY 环境变量，无法初始化 OpenAI 兼容 provider")
        kwargs: dict = {"api_key": key}
        if config.openai_base_url:
            kwargs["base_url"] = config.openai_base_url
        self._client = openai.AsyncOpenAI(**kwargs)
        self._model = config.model
        self._max_tokens = config.max_tokens
        self._thinking_effort = config.thinking_effort

    async def chat(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
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
        return self._parse(resp)

    def tool_result_message(self, call: ToolCall, result: str) -> dict:
        return {"role": "tool", "tool_call_id": call.call_id, "content": result}

    @staticmethod
    def _parse(resp: openai.types.chat.ChatCompletion) -> LLMResponse:
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
