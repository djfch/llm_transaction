"""思考程度（thinking_effort）翻译测试：纯函数矩阵 + 三个 provider 的 wire 组装。

纯函数部分覆盖 档位 × 模型前缀 全矩阵与 anthropic 预算映射；provider 层用
fake client（不触网）验证翻译结果真的进入请求体。既有测试文件
（test_openai_responses.py）继续覆盖解析/回放，本文件只测 thinking 注入。
"""

from types import SimpleNamespace

import pytest

from src.agent.providers.anthropic import AnthropicProvider
from src.agent.providers.openai_compat import OpenAICompatProvider
from src.agent.providers.openai_responses import OpenAIResponsesProvider
from src.agent.providers.thinking import anthropic_thinking, thinking_wire_kwargs
from src.config import CredentialConfig


# ---------- 纯函数：thinking_wire_kwargs（OpenAI 兼容通道） ----------


def test_thinking_empty_and_on_default_to_no_params():
    """空串（跟随模型默认）与 on 对默认思考的模型都不传参数；on 对 qwen 显式开启。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    assert thinking_wire_kwargs("deepseek-v4-flash", "") == {}
    assert thinking_wire_kwargs("deepseek-v4-flash", "on") == {}
    assert thinking_wire_kwargs("gpt-5.6", "on") == {}
    assert thinking_wire_kwargs("qwen3.7-plus", "on") == {"extra_body": {"enable_thinking": True}}


def test_thinking_off_per_model_family():
    """off 按模型前缀分派：qwen 走 enable_thinking；deepseek/旧 kimi k2 走 thinking 对象；

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    assert thinking_wire_kwargs("qwen-plus", "off") == {"extra_body": {"enable_thinking": False}}
    assert thinking_wire_kwargs("deepseek-v4-pro", "off") == {
        "extra_body": {"thinking": {"type": "disabled"}}
    }
    assert thinking_wire_kwargs("kimi-k2.6", "off") == {
        "extra_body": {"thinking": {"type": "disabled"}}
    }
    assert thinking_wire_kwargs("glm-5.2", "off") == {"reasoning_effort": "none"}
    assert thinking_wire_kwargs("gpt-5.6", "off") == {"reasoning_effort": "none"}
    assert thinking_wire_kwargs("kimi-k3", "off") == {"reasoning_effort": "none"}


def test_thinking_effort_passthrough_per_model_family():
    """档位原样透传 reasoning_effort（DeepSeek/GLM/Kimi k3/GPT 通用字段）；

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    for model in ("deepseek-v4-pro", "glm-5.2", "kimi-k3", "gpt-5.6"):
        assert thinking_wire_kwargs(model, "high") == {"reasoning_effort": "high"}
    assert thinking_wire_kwargs("qwen3.8-max", "xhigh") == {"extra_body": {"enable_thinking": True}}
    assert thinking_wire_kwargs("kimi-k2.6", "high") == {
        "extra_body": {"thinking": {"type": "enabled"}}
    }
    assert thinking_wire_kwargs("kimi-k2.5", "high") == {
        "extra_body": {"thinking": {"type": "enabled"}}
    }


def test_thinking_always_thinking_model_off_degrades():
    """kimi-k2.7 系始终思考官方不可关：off 降级为不传；档位仍走 reasoning_effort。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    assert thinking_wire_kwargs("kimi-k2.7-code", "off") == {}
    assert thinking_wire_kwargs("kimi-k2.7-code-highspeed", "off") == {}
    assert thinking_wire_kwargs("kimi-k2.7-code", "high") == {"reasoning_effort": "high"}


def test_thinking_model_name_case_insensitive():
    """模型名大小写不敏感（与厂商端点命名惯例对齐）。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    assert thinking_wire_kwargs("QWEN-Plus", "off") == {"extra_body": {"enable_thinking": False}}
    assert thinking_wire_kwargs("DEEPSEEK-V4-PRO", "off") == {
        "extra_body": {"thinking": {"type": "disabled"}}
    }
    assert thinking_wire_kwargs("GPT-5.6", "high") == {"reasoning_effort": "high"}


# ---------- 纯函数：anthropic_thinking（Anthropic 通道） ----------


def test_anthropic_thinking_off_and_default_omit():
    """空串/off 不传 thinking（Claude 默认不思考）。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    assert anthropic_thinking("", 4096) is None
    assert anthropic_thinking("off", 4096) is None


def test_anthropic_thinking_effort_maps_to_budget():
    """on/档位 → enabled + budget_tokens（档位映射 token 预算；max_tokens 足够大时映射值原样生效）。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    assert anthropic_thinking("on", 4096) == {"type": "enabled", "budget_tokens": 3072}
    assert anthropic_thinking("low", 4096) == {"type": "enabled", "budget_tokens": 3072}
    assert anthropic_thinking("high", 65536) == {"type": "enabled", "budget_tokens": 16000}
    assert anthropic_thinking("xhigh", 65536) == {"type": "enabled", "budget_tokens": 24000}
    assert anthropic_thinking("max", 65536) == {"type": "enabled", "budget_tokens": 32000}


def test_anthropic_thinking_budget_clamped_by_max_tokens():
    """budget 必须严格小于 max_tokens（官方约束）：按 max_tokens 裁剪；

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    assert anthropic_thinking("max", 5000) == {"type": "enabled", "budget_tokens": 3976}
    assert anthropic_thinking("high", 2048) == {"type": "enabled", "budget_tokens": 1024}
    assert anthropic_thinking("high", 2047) is None
    assert anthropic_thinking("on", 1000) is None


# ---------- provider 层：openai_compat ----------


class _FakeMessage(SimpleNamespace):
    def model_dump(self, exclude_none: bool = False) -> dict:
        """模拟 SDK 消息对象的序列化：属性字典化，可选剔除 None 字段。

        参数：
            self: _FakeMessage，当前测试替身实例
            exclude_none: bool，是否剔除空字段
        返回：
            dict，返回该测试辅助函数构造或记录的结果
        """
        return (
            {k: v for k, v in self.__dict__.items() if v is not None}
            if exclude_none
            else dict(self.__dict__)
        )


class _FakeCompletions:
    def __init__(self) -> None:
        """初始化录制器：准备空列表承接 create 调用录下的请求体。

        参数：
            self: _FakeCompletions，当前测试替身实例
        返回：
            None，初始化并保存测试替身状态
        """
        self.requests: list[dict] = []

    async def create(self, **kwargs):
        """冒充 chat.completions.create：录下请求体并返回最小可用的假响应。

        参数：
            self: _FakeCompletions，当前测试替身实例
            **kwargs: dict[str, object]，透传的关键字参数
        返回：
            SimpleNamespace，最小可用的聊天补全响应
        """
        self.requests.append(kwargs)
        msg = _FakeMessage(content="ok", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], model_dump_json=lambda: "{}")


class _FakeChatAPI:
    def __init__(self) -> None:
        """组装假 chat 命名空间：挂上 completions 录制器。

        参数：
            self: _FakeChatAPI，当前测试替身实例
        返回：
            None，初始化并保存测试替身状态
        """
        self.completions = _FakeCompletions()


class _FakeOpenAIClient:
    def __init__(self, api: _FakeChatAPI) -> None:
        """组装假 OpenAI client：把假 chat 命名空间挂到 .chat 属性。

        参数：
            self: _FakeOpenAIClient，当前测试替身实例
            api: _FakeChatAPI，伪造的 SDK 接口
        返回：
            None，初始化并保存测试替身状态
        """
        self.chat = api


def _compat_provider(api: _FakeChatAPI, model: str, effort: str) -> OpenAICompatProvider:
    """构造一个内部 client 已换成假 client 的 OpenAICompatProvider，用于观察 wire 请求体。

    参数：
        api: _FakeChatAPI，伪造的 SDK 接口
        model: str，模型名称
        effort: str，推理强度
    返回：
        OpenAICompatProvider，返回该测试辅助函数构造或记录的结果
    """
    cred = CredentialConfig(name="t", provider="openai_compat", model=model, thinking_effort=effort)
    provider = OpenAICompatProvider(cred, api_key="sk-test")
    provider._client = _FakeOpenAIClient(api)
    return provider


@pytest.mark.asyncio
async def test_compat_wire_effort_and_extra_body():
    """验证兼容接口按模型分别映射 DeepSeek 思考程度与 Qwen 思考开关。

    参数：无
    返回：
        None，通过断言验证 DeepSeek 使用 reasoning_effort，Qwen 使用 enable_thinking
    """
    api = _FakeChatAPI()
    await _compat_provider(api, "deepseek-v4-pro", "high").chat(
        "s", [{"role": "user", "content": "hi"}], []
    )
    req = api.completions.requests[0]
    assert req["reasoning_effort"] == "high"
    assert "extra_body" not in req

    api2 = _FakeChatAPI()
    await _compat_provider(api2, "qwen-plus", "off").chat("s", [], [])
    assert api2.completions.requests[0]["extra_body"] == {"enable_thinking": False}
    assert "reasoning_effort" not in api2.completions.requests[0]

    api3 = _FakeChatAPI()
    await _compat_provider(api3, "deepseek-v4-flash", "").chat("s", [], [])
    req3 = api3.completions.requests[0]
    assert "reasoning_effort" not in req3
    assert "extra_body" not in req3
    assert req3["model"] == "deepseek-v4-flash"


# ---------- provider 层：openai_responses ----------


class _FakeResponses:
    def __init__(self) -> None:
        """初始化录制器：准备空列表承接 responses.create 录下的请求体。

        参数：
            self: _FakeResponses，当前测试替身实例
        返回：
            None，初始化并保存测试替身状态
        """
        self.requests: list[dict] = []

    async def create(self, **kwargs):
        """冒充 responses.create：录下请求体并返回最小可用的假响应。

        参数：
            self: _FakeResponses，当前测试替身实例
            **kwargs: dict[str, object]，透传的关键字参数
        返回：
            SimpleNamespace，最小可用的 Responses API 响应
        """
        self.requests.append(kwargs)
        return SimpleNamespace(
            output=[], status="completed", incomplete_details=None, model_dump_json=lambda: "{}"
        )


class _FakeResponsesClient:
    def __init__(self, api: _FakeResponses) -> None:
        """组装假 Responses client：把假 responses 命名空间挂到 .responses 属性。

        参数：
            self: _FakeResponsesClient，当前测试替身实例
            api: _FakeResponses，伪造的 SDK 接口
        返回：
            None，初始化并保存测试替身状态
        """
        self.responses = api


def _responses_provider(api: _FakeResponses, effort: str) -> OpenAIResponsesProvider:
    """构造一个内部 client 已换成假 client 的 OpenAIResponsesProvider，用于观察 wire 请求体。

    参数：
        api: _FakeResponses，伪造的 SDK 接口
        effort: str，推理强度
    返回：
        OpenAIResponsesProvider，返回该测试辅助函数构造或记录的结果
    """
    cred = CredentialConfig(
        name="t", provider="openai_responses", model="gpt-5.6", thinking_effort=effort
    )
    provider = OpenAIResponsesProvider(cred, api_key="sk-test")
    provider._client = _FakeResponsesClient(api)
    return provider


@pytest.mark.asyncio
async def test_responses_wire_reasoning_object():
    """档位 → reasoning={effort, summary: auto} + include；off → effort none；

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    api = _FakeResponses()
    await _responses_provider(api, "high").chat("s", [{"role": "user", "content": "hi"}], [])
    req = api.requests[0]
    assert req["reasoning"] == {"effort": "high", "summary": "auto"}
    assert req["include"] == ["reasoning.encrypted_content"]

    api2 = _FakeResponses()
    await _responses_provider(api2, "off").chat("s", [], [])
    assert api2.requests[0]["reasoning"]["effort"] == "none"

    api3 = _FakeResponses()
    await _responses_provider(api3, "").chat("s", [], [])
    assert "reasoning" not in api3.requests[0]
    assert "include" not in api3.requests[0]

    api4 = _FakeResponses()
    await _responses_provider(api4, "on").chat("s", [], [])
    assert "reasoning" not in api4.requests[0]


# ---------- provider 层：anthropic ----------


class _FakeAnthropicBlock(SimpleNamespace):
    def model_dump(self) -> dict:
        """将测试响应对象序列化为字典。

        参数：
            self: _FakeAnthropicBlock，当前测试替身实例
        返回：
            dict，返回该测试辅助函数构造或记录的结果
        """
        return dict(self.__dict__)


class _FakeAnthropicMessages:
    def __init__(self) -> None:
        """初始化测试替身及其可观测状态。

        参数：
            self: _FakeAnthropicMessages，当前测试替身实例
        返回：
            None，初始化并保存测试替身状态
        """
        self.requests: list[dict] = []

    async def create(self, **kwargs):
        """记录请求参数并返回伪造的接口响应。

        参数：
            self: _FakeAnthropicMessages，当前测试替身实例
            **kwargs: dict[str, object]，透传的关键字参数
        返回：
            SimpleNamespace，最小可用的 Anthropic 消息响应
        """
        self.requests.append(kwargs)
        return SimpleNamespace(
            content=[_FakeAnthropicBlock(type="text", text="ok")], model_dump_json=lambda: "{}"
        )


class _FakeAnthropicClient:
    def __init__(self, messages: _FakeAnthropicMessages) -> None:
        """初始化测试替身及其可观测状态。

        参数：
            self: _FakeAnthropicClient，当前测试替身实例
            messages: _FakeAnthropicMessages，对话消息列表
        返回：
            None，初始化并保存测试替身状态
        """
        self.messages = messages


def _anthropic_provider(
    messages: _FakeAnthropicMessages, effort: str, max_tokens: int = 4096
) -> AnthropicProvider:
    """构造使用伪造消息接口的 Anthropic 提供方。

    参数：
        messages: _FakeAnthropicMessages，对话消息列表
        effort: str，推理强度
        max_tokens: int，最大输出令牌数
    返回：
        AnthropicProvider，返回该测试辅助函数构造或记录的结果
    """
    cred = CredentialConfig(
        name="t",
        provider="anthropic",
        model="claude-sonnet-4-5",
        max_tokens=max_tokens,
        thinking_effort=effort,
    )
    provider = AnthropicProvider(cred, api_key="sk-test")
    provider._client = _FakeAnthropicClient(messages)
    return provider


@pytest.mark.asyncio
async def test_anthropic_wire_thinking_budget():
    """high → thinking enabled + budget（按 max_tokens 裁剪，严格小于上限）；

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    api = _FakeAnthropicMessages()
    await _anthropic_provider(api, "high", max_tokens=20000).chat(
        "s", [{"role": "user", "content": "hi"}], []
    )
    assert api.requests[0]["thinking"] == {"type": "enabled", "budget_tokens": 16000}

    api2 = _FakeAnthropicMessages()
    await _anthropic_provider(api2, "high").chat("s", [], [])  # 默认 max_tokens=4096
    assert api2.requests[0]["thinking"] == {"type": "enabled", "budget_tokens": 3072}

    api3 = _FakeAnthropicMessages()
    await _anthropic_provider(api3, "").chat("s", [], [])
    assert "thinking" not in api3.requests[0]

    api4 = _FakeAnthropicMessages()
    await _anthropic_provider(api4, "off").chat("s", [], [])
    assert "thinking" not in api4.requests[0]
