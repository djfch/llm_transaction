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
        """初始化 OpenAI Responses 客户端：取 key、建 AsyncOpenAI 并记下模型参数。

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
            raise LLMError("缺少 OPENAI_API_KEY 环境变量，无法初始化 OpenAI Responses provider")
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
        """调用 OpenAI Responses API 发起一轮对话，返回统一格式的回复。

        参数：
            system: str，系统提示词（作为 instructions 传给模型）
            messages: list[dict]，内部多轮对话消息列表（普通 user/assistant 消息、
                带 response_items 的 assistant 私有包裹、function_call_output 工具结果），
                发送前由 _to_input 展开回放为原生 input 项
            tools: list[dict]，中性格式工具定义 {name, description, parameters(JSON Schema)}，
                内部转换为 Responses 扁平 function 格式；空列表表示不带工具

        返回：
            LLMResponse：统一回复（文本 + 工具调用列表 + 原始输出 + 原生 assistant 消息）

        异常：
            LLMError：OpenAI API 调用失败（网络/鉴权/限流/服务端错误等）时抛出
        """
        oai_tools = [
            {
                "type": "function",
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            }
            for t in tools
        ]
        request: dict = {
            "model": self._model,
            "max_output_tokens": self._max_tokens,
            "instructions": system,
            "input": self._to_input(messages),
            "tools": oai_tools or None,
        }
        # 思考程度：空串/on 不传（用模型默认）；off 显式 none（默认思考的模型
        # 如 GPT-5.6 不传关不掉）；档位原样透传。summary: auto 拿推理摘要，
        # include 显式请求 reasoning 内容：无状态回放（工具调用多轮）时
        # 历史 reasoning 项需随 input 回传，服务端才保留完整推理上下文
        effort = self._thinking_effort
        if effort and effort != "on":
            request["reasoning"] = {
                "effort": "none" if effort == "off" else effort,
                "summary": "auto",
            }
            request["include"] = ["reasoning.encrypted_content"]
        try:
            resp = await self._client.responses.create(**request)
        except openai.APIError as e:
            raise LLMError(f"OpenAI Responses API 错误: {e}") from e
        raw = resp.model_dump_json()
        try:
            return self._parse(resp)
        except Exception as exc:
            setattr(exc, "raw", raw)
            raise

    def tool_result_message(self, call: ToolCall, result: str) -> dict:
        """把一次工具执行结果包装成 Responses 原生 function_call_output 项，供回填继续对话。

        参数：
            call: ToolCall，对应的工具调用（取 call_id 关联原调用）
            result: str，工具执行结果文本

        返回：
            dict：{"type": "function_call_output", "call_id", "output"} 形式的消息
        """
        return {"type": "function_call_output", "call_id": call.call_id, "output": result}

    @staticmethod
    def _to_input(messages: list[dict]) -> list[dict]:
        """内部 messages → Responses input 数组。

        三种形态：带 response_items 键的 assistant 私有包裹展开逐项追加；
        function_call_output 工具结果透传；其余普通 user/assistant 消息原样透传。

        参数：
            messages: list[dict]，统一格式的对话消息

        返回：
            list[dict]，符合 Responses API 输入格式的消息数组

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

        参数：
            resp: Response，提供商原始响应

        返回：
            LLMResponse，output 项列表 → 统一 LLMResponse；reasoning 等其他类型项跳过。  status 非 completed（截断/失败）抛 LLMError：截断轮 output 可能全空， 放行会被当作"空仓观望"落库 ok=True；SDK status 为 Optional，None 放行

        异常：
            LLMError，Responses 状态不是 completed 时抛出并阻止本轮交易

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

        参数：
            item: object，提供商响应中的工具调用项

        返回：
            ToolCall，包含工具名、对象参数与 call_id 的统一工具调用

        异常：
            LLMParseError，工具参数不是合法 JSON 或解析后不是对象时抛出

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
