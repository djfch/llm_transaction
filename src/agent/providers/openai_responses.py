"""OpenAI Responses API 实现：官方 AsyncOpenAI SDK 的 client.responses.create。

与 chat.completions 的 OpenAICompatProvider 并列，key 同样从 OPENAI_API_KEY 读取，
base_url 可配（LLMConfig.openai_base_url）。采用无状态 input 回放（不用
previous_response_id）：assistant 回合以私有包裹 {"role":"assistant",
"response_items":[...]} 存进 messages，回放时展开为逐项原生 output 项；
工具结果以 {"type":"function_call_output","call_id","output"} 回填。
工具定义为扁平 {"type":"function","name","description","parameters"}（不嵌套
function 子对象）；工具参数 arguments 为 JSON 字符串，非法 JSON 或非对象抛
LLMParseError（本轮不交易）。响应 status 非 completed（如 max_output_tokens
撞顶的 incomplete，reasoning 模型的推理 token 也吃该预算，可致 output 全空）
抛 LLMError 计入连续失败：不把"模型没思考完"伪装成正常观望轮。
"""

from __future__ import annotations

import json
import os

import openai
from openai.types.responses import Response

from src.agent.providers.base import LLMError, LLMParseError, LLMResponse, ToolCall
from src.config import CredentialConfig, LLMConfig


class OpenAIResponsesProvider:
    """OpenAI Responses API 适配（无状态 input 回放）。"""

    def __init__(self, config: LLMConfig | CredentialConfig, api_key: str | None = None) -> None:
        key = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not key:
            raise LLMError("缺少 OPENAI_API_KEY 环境变量，无法初始化 OpenAI Responses provider")
        kwargs: dict = {"api_key": key}
        if config.openai_base_url:
            kwargs["base_url"] = config.openai_base_url
        self._client = openai.AsyncOpenAI(**kwargs)
        self._model = config.model
        self._max_tokens = config.max_tokens

    async def chat(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        oai_tools = [
            {
                "type": "function",
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            }
            for t in tools
        ]
        try:
            resp = await self._client.responses.create(
                model=self._model,
                max_output_tokens=self._max_tokens,
                instructions=system,
                input=self._to_input(messages),
                tools=oai_tools or None,
            )
        except openai.APIError as e:
            raise LLMError(f"OpenAI Responses API 错误: {e}") from e
        return self._parse(resp)

    def tool_result_message(self, call: ToolCall, result: str) -> dict:
        return {"type": "function_call_output", "call_id": call.call_id, "output": result}

    @staticmethod
    def _to_input(messages: list[dict]) -> list[dict]:
        """内部 messages → Responses input 数组。

        三种形态：带 response_items 键的 assistant 私有包裹展开逐项追加；
        function_call_output 工具结果透传；其余普通 user/assistant 消息原样透传。
        """
        items: list[dict] = []
        for msg in messages:
            if "response_items" in msg:
                items.extend(msg["response_items"])
            else:
                items.append(msg)
        return items

    @staticmethod
    def _parse(resp: Response) -> LLMResponse:
        """output 项列表 → 统一 LLMResponse；reasoning 等其他类型项跳过。

        status 非 completed（截断/失败）抛 LLMError：截断轮 output 可能全空，
        放行会被当作"空仓观望"落库 ok=True；SDK status 为 Optional，None 放行。
        """
        status = getattr(resp, "status", None)
        if status not in (None, "completed"):
            reason = getattr(getattr(resp, "incomplete_details", None), "reason", None) or status
            raise LLMError(
                f"OpenAI Responses 未完成（status={status}, reason={reason}），本轮不交易"
            )
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for item in resp.output:
            if item.type == "message":
                text_parts.extend(c.text for c in item.content if c.type == "output_text")
            elif item.type == "function_call":
                calls.append(OpenAIResponsesProvider._parse_call(item))
        return LLMResponse(
            text="".join(text_parts),
            tool_calls=calls,
            raw=resp.model_dump_json(),
            assistant_message={
                "role": "assistant",
                "response_items": [item.model_dump(exclude_none=True) for item in resp.output],
            },
        )

    @staticmethod
    def _parse_call(item) -> ToolCall:
        """function_call 项 → ToolCall；call_id 取 call_id 字段（不是 id）。

        arguments 非法 JSON 或非 JSON 对象抛 LLMParseError（本轮不交易）。
        """
        try:
            args = json.loads(item.arguments) if item.arguments else {}
        except json.JSONDecodeError as e:
            raise LLMParseError(
                f"工具 {item.name} 参数不是合法 JSON: {item.arguments[:200]}"
            ) from e
        if not isinstance(args, dict):
            raise LLMParseError(f"工具 {item.name} 参数必须是 JSON 对象")
        return ToolCall(name=item.name, args=args, call_id=item.call_id)
