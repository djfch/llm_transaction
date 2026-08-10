"""OpenAI Responses provider 的测试：fake client 仿造 resp.output，不触网。

覆盖：chat 请求组装（instructions/input/扁平 tools/max_output_tokens）、
_parse 文本与 function_call 解析（含 LLMParseError 防护、incomplete 状态
fail-closed、空串 arguments）、tool_result_message 形状、_to_input 三种形态
回放、base_url 传入、create_provider 按 openai_responses 凭证构造。
"""

from types import SimpleNamespace

import httpx
import openai
import pytest

from src.agent.providers.base import LLMError, LLMParseError, ToolCall
from src.agent.providers.factory import create_provider
from src.agent.providers.openai_responses import OpenAIResponsesProvider
from src.agent.providers.retry import RetryingProvider
from src.config import CredentialConfig


class _FakeOutputItem(SimpleNamespace):
    """仿造 SDK output 项（pydantic 模型的 model_dump）。"""

    def model_dump(self, exclude_none: bool = False) -> dict:
        """仿造 pydantic 的 model_dump，将实例属性序列化为字典。

        参数：
            exclude_none: bool，为 True 时剔除值为 None 的字段，与 pydantic 行为对齐

        返回：
            dict：实例属性字典，exclude_none 为 True 时不含 None 字段
        """
        data = dict(self.__dict__)
        # 与 pydantic model_dump(exclude_none=True) 行为对齐：剔除 None 字段，
        # 保持回放夹具与实际请求序列化一致，才能验证 None 字段不会进入请求
        return {k: v for k, v in data.items() if v is not None} if exclude_none else data


class _FakeResponse:
    def __init__(
        self, output: list, status: str | None = None, incomplete_reason: str = ""
    ) -> None:
        """构造仿造的 Responses 响应对象。

        参数：
            output: list，仿造的 output 输出项列表
            status: str | None，响应状态（如 "incomplete"），None 表示正常完成
            incomplete_reason: str，截断原因（如 "max_output_tokens"），空串表示无截断详情

        返回：
            None，就地设置 self.output/self.status/self.incomplete_details 属性
        """
        self.output = output
        self.status = status
        self.incomplete_details = (
            SimpleNamespace(reason=incomplete_reason) if incomplete_reason else None
        )

    def model_dump_json(self) -> str:
        """仿造 pydantic 的 model_dump_json，返回固定空 JSON 串作为原始响应留痕。

        参数：无

        返回：
            str：固定为 "{}"，仅用于填充解析结果的 raw 字段
        """
        return "{}"


class _FakeResponsesAPI:
    """仿造 client.responses：记录 create 请求参数，返回预定响应或抛错。"""

    def __init__(self, resp: _FakeResponse | None = None, error: Exception | None = None) -> None:
        """构造仿造的 responses 接口。

        参数：
            resp: _FakeResponse | None，create 调用时返回的预定响应
            error: Exception | None，create 调用时抛出的预定异常，优先级高于 resp

        返回：
            None，就地初始化预定响应、预定异常与请求记录列表
        """
        self._resp = resp
        self._error = error
        self.requests: list[dict] = []

    async def create(self, **kwargs):
        """记录请求参数并返回预置响应或抛出预置异常。

        参数：
            **kwargs: object，Responses API 请求参数

        返回：
            object：未配置异常时返回的预置响应对象

        异常：
            Exception：构造假接口时配置了 error 时原样抛出该异常
        """
        self.requests.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._resp


class _FakeClient:
    def __init__(self, api: _FakeResponsesAPI) -> None:
        """把预置 Responses API 暴露为客户端的 responses 属性。

        参数：
            api: _FakeResponsesAPI，记录请求并返回预置响应的假接口

        返回：
            None：保存假接口供提供者通过 client.responses 调用
        """
        self.responses = api


def _message_item(*texts: str) -> _FakeOutputItem:
    """把多段输出文本包装成 Responses API 消息条目。

    参数：
    *texts: str，依次写入消息 content 的输出文本

    返回：
    _FakeOutputItem：type 为 message 且包含各 output_text 子项的假响应条目
    """
    content = [_FakeOutputItem(type="output_text", text=t) for t in texts]
    return _FakeOutputItem(type="message", content=content)


def _function_call_item(arguments: str = '{"symbol": "BTC"}') -> _FakeOutputItem:
    """构造调用 get_price 的 Responses API 函数调用条目。

    参数：
    arguments: str，函数调用携带的 JSON 参数文本

    返回：
    _FakeOutputItem：带固定调用编号、函数名及指定参数的假函数调用条目
    """
    return _FakeOutputItem(
        type="function_call", name="get_price", arguments=arguments, call_id="call_1", id="fc_1"
    )


def _cred(**kwargs) -> CredentialConfig:
    """构造测试用的 LLM 凭据配置。

    参数：
    **kwargs: object，覆盖默认凭据字段的关键字参数

    返回：
    CredentialConfig：provider 为 openai_responses、模型为 gpt-5 的测试凭据
    """
    return CredentialConfig(name="t", provider="openai_responses", model="gpt-5", **kwargs)


def _provider(api: _FakeResponsesAPI, **cred_kwargs) -> OpenAIResponsesProvider:
    """组装 OpenAI Responses 提供者测试替身。

    参数：
    api: _FakeResponsesAPI，注入提供者客户端的假 Responses 接口
    **cred_kwargs: object，覆盖默认测试凭据的关键字参数

    返回：
    OpenAIResponsesProvider：使用测试密钥并替换了真实客户端的提供者
    """
    provider = OpenAIResponsesProvider(_cred(**cred_kwargs), api_key="sk-test")
    provider._client = _FakeClient(api)
    return provider


# ---------- chat 请求组装 ----------


async def test_chat_assembles_responses_request():
    """system→instructions、max_tokens→max_output_tokens、tools 为扁平格式。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    api = _FakeResponsesAPI(resp=_FakeResponse([_message_item("看多")]))
    provider = _provider(api, max_tokens=123)
    tools = [{"name": "get_price", "description": "查价", "parameters": {"type": "object"}}]
    out = await provider.chat("你是交易员", [{"role": "user", "content": "hi"}], tools)
    req = api.requests[0]
    assert req["model"] == "gpt-5"
    assert req["max_output_tokens"] == 123
    assert req["instructions"] == "你是交易员"
    assert req["input"] == [{"role": "user", "content": "hi"}]
    assert req["tools"] == [
        {
            "type": "function",
            "name": "get_price",
            "description": "查价",
            "parameters": {"type": "object"},
        }
    ]
    assert out.text == "看多"


async def test_chat_without_tools_omits_tools():
    """无工具时 tools 传 None（等价省略）。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    api = _FakeResponsesAPI(resp=_FakeResponse([]))
    await _provider(api).chat("s", [], [])
    assert api.requests[0]["tools"] is None


async def test_chat_api_error_wrapped_as_llmerror():
    """openai.APIError 包装为 LLMError。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    err = openai.APIError(
        "boom", request=httpx.Request("POST", "https://api.openai.com/v1/responses"), body=None
    )
    with pytest.raises(LLMError, match="OpenAI Responses API 错误"):
        await _provider(_FakeResponsesAPI(error=err)).chat("s", [], [])


# ---------- _parse 解析 ----------


def test_parse_text_and_function_call():
    """message 项收集 output_text，function_call 项转 ToolCall（call_id 取 call_id 字段而非 id），
    reasoning 等其他类型项跳过。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    resp = _FakeResponse(
        [_FakeOutputItem(type="reasoning"), _message_item("先看", "价格"), _function_call_item()]
    )
    out = OpenAIResponsesProvider._parse(resp)
    assert out.text == "先看价格"
    assert out.tool_calls == [ToolCall(name="get_price", args={"symbol": "BTC"}, call_id="call_1")]
    assert out.raw == "{}"
    assert out.assistant_message["role"] == "assistant"
    assert out.assistant_message["response_items"] == [
        {"type": "reasoning"},
        _message_item("先看", "价格").model_dump(),
        _function_call_item().model_dump(),
    ]


def test_parse_invalid_arguments_json_raises():
    """arguments 非法 JSON → LLMParseError（本轮不交易）。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    resp = _FakeResponse([_function_call_item(arguments="not-json{")])
    with pytest.raises(LLMParseError, match="不是合法 JSON"):
        OpenAIResponsesProvider._parse(resp)


def test_parse_non_object_arguments_raises():
    """arguments 是合法 JSON 但非对象 → LLMParseError。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    resp = _FakeResponse([_function_call_item(arguments='["BTC"]')])
    with pytest.raises(LLMParseError, match="必须是 JSON 对象"):
        OpenAIResponsesProvider._parse(resp)


def test_parse_empty_arguments_treated_as_no_args():
    """arguments 为空串（无参工具的真实形状）→ args={}。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    resp = _FakeResponse([_function_call_item(arguments="")])
    out = OpenAIResponsesProvider._parse(resp)
    assert out.tool_calls == [ToolCall(name="get_price", args={}, call_id="call_1")]


def test_parse_incomplete_status_raises_llmerror():
    """max_output_tokens 撞顶（status=incomplete，output 可全空）→ LLMError 计入连续失败，
    不把截断轮伪装成正常观望（reasoning 模型的推理 token 也吃该预算）。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    resp = _FakeResponse([], status="incomplete", incomplete_reason="max_output_tokens")
    with pytest.raises(LLMError, match="reason=max_output_tokens"):
        OpenAIResponsesProvider._parse(resp)


# ---------- tool_result_message / _to_input 回放 ----------


def test_tool_result_message_shape():
    """工具结果回填为 function_call_output 形状。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    msg = _provider(_FakeResponsesAPI()).tool_result_message(
        ToolCall(name="get_price", args={}, call_id="call_9"), "65000"
    )
    assert msg == {"type": "function_call_output", "call_id": "call_9", "output": "65000"}


def test_to_input_replays_three_message_forms():
    """三种形态：普通消息透传；assistant 私有包裹展开逐项；function_call_output 透传。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "response_items": [{"type": "message", "a": 1}, {"type": "function_call", "b": 2}],
        },
        {"type": "function_call_output", "call_id": "call_1", "output": "ok"},
    ]
    assert OpenAIResponsesProvider._to_input(messages) == [
        {"role": "user", "content": "hi"},
        {"type": "message", "a": 1},
        {"type": "function_call", "b": 2},
        {"type": "function_call_output", "call_id": "call_1", "output": "ok"},
    ]


# ---------- 构造与工厂 ----------


def test_base_url_passed_to_client():
    """openai_base_url 非空时传入 AsyncOpenAI，留空时使用官方端点。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    provider = OpenAIResponsesProvider(
        _cred(openai_base_url="https://proxy.example.com/v1"), api_key="sk-test"
    )
    assert str(provider._client.base_url).startswith("https://proxy.example.com/v1")


def test_missing_key_raises_llmerror(monkeypatch: pytest.MonkeyPatch):
    """缺 key 抛 LLMError，错误消息点名 OPENAI_API_KEY。

    参数：
        monkeypatch: pytest.MonkeyPatch，pytest 提供的动态补丁夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(LLMError, match="OPENAI_API_KEY"):
        OpenAIResponsesProvider(_cred())


def test_create_provider_builds_openai_responses(monkeypatch: pytest.MonkeyPatch):
    """openai_responses 凭证经 create_provider 构造出 OpenAIResponsesProvider（外裹重试装饰器）。

    参数：
        monkeypatch: pytest.MonkeyPatch，pytest 提供的动态补丁夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    monkeypatch.setenv("LLM_KEY_T", "sk-test")
    provider = create_provider(_cred())
    assert isinstance(provider, RetryingProvider)
    assert isinstance(provider._inner, OpenAIResponsesProvider)
