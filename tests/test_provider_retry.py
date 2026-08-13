"""RetryingProvider 同参重试装饰器：失败重发语义、参数不变性、取消传播。"""

from __future__ import annotations

import asyncio
import copy

import pytest

from src.agent.providers.anthropic import AnthropicProvider
from src.agent.providers.base import LLMError, LLMParseError, LLMResponse, ToolCall
from src.agent.providers.factory import create_provider
from src.agent.providers.openai_compat import OpenAICompatProvider
from src.agent.providers.openai_responses import OpenAIResponsesProvider
from src.agent.providers.retry import RetryingProvider
from src.config import CredentialConfig

_OK = LLMResponse(text="好", raw="raw-ok", assistant_message={"role": "assistant", "content": "好"})

_MESSAGES = [{"role": "user", "content": "上下文"}]
_TOOLS = [{"name": "t", "description": "d", "parameters": {}}]


class _ScriptedProvider:
    """按脚本逐次抛出/返回的假 provider；记录每次收到的参数快照。"""

    def __init__(self, script: list) -> None:
        """初始化假 provider，保存待消费脚本与空调用记录。

        参数：
            script: list，逐次消费的脚本，元素为要抛出的异常实例或要返回的 LLMResponse

        返回：
            None，就地保存脚本副本并初始化空的调用参数记录列表
        """
        self._script = list(script)
        self.calls: list[tuple[str, list[dict], list[dict]]] = []

    async def chat(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        """记录本次请求快照并按脚本弹出响应或异常。

        参数：
            system: str，系统提示词
            messages: list[dict]，对话消息列表
            tools: list[dict]，可调用工具定义

        返回：
            LLMResponse，脚本队首预置的模型响应

        异常：
            BaseException: 脚本队首为异常实例时原样抛出
        """
        self.calls.append((system, copy.deepcopy(messages), copy.deepcopy(tools)))
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def tool_result_message(self, call: ToolCall, result: str) -> dict:
        """把工具结果包装为关联原调用编号的消息字典。

        参数：
            call: ToolCall，已执行的工具调用
            result: str，工具执行结果文本

        返回：
            dict，provider 约定的 tool 角色消息
        """
        return {"role": "tool", "tool_call_id": call.call_id, "content": result}


def _wrap(inner: _ScriptedProvider) -> RetryingProvider:
    """使用零退避配置包装脚本 provider，避免单元测试真实等待。

    参数：
        inner: _ScriptedProvider，负责按脚本返回或抛错的底层 provider

    返回：
        RetryingProvider，最多三次同参尝试且重试间隔为零的包装器
    """
    return RetryingProvider(inner, backoff=(0, 0))


async def test_success_after_two_transport_errors() -> None:
    """验证两次传输错误后第三次成功，并保持三次调用参数完全相同。

    参数：无

    返回：
        None，通过断言验证重试次数、最终响应与同参不变量
    """
    inner = _ScriptedProvider([LLMError("连接超时"), LLMError("502"), _OK])
    resp = await _wrap(inner).chat("sys", _MESSAGES, _TOOLS)
    assert resp is _OK
    assert len(inner.calls) == 3
    assert inner.calls[0] == inner.calls[1] == inner.calls[2] == ("sys", _MESSAGES, _TOOLS)


async def test_caller_arguments_not_mutated() -> None:
    """验证重试过程不会修改调用方传入的消息与工具列表。

    参数：无

    返回：
        None，通过断言验证两个可变参数仍保持原值
    """
    messages = [{"role": "user", "content": "原始"}]
    tools = [{"name": "t", "description": "d", "parameters": {}}]
    inner = _ScriptedProvider([LLMError("x"), _OK])
    await _wrap(inner).chat("sys", messages, tools)
    assert messages == [{"role": "user", "content": "原始"}]
    assert tools == [{"name": "t", "description": "d", "parameters": {}}]


async def test_three_llm_errors_raise_original() -> None:
    """验证三次模型错误耗尽重试后抛出最后一次的原类型与原消息。

    参数：无

    返回：
        None，通过断言验证最终异常和调用次数
    """
    inner = _ScriptedProvider([LLMError("e1"), LLMError("e2"), LLMError("e3")])
    with pytest.raises(LLMError, match="e3"):
        await _wrap(inner).chat("sys", _MESSAGES, _TOOLS)
    assert len(inner.calls) == 3


async def test_parse_error_retried_same_params_then_raise() -> None:
    """验证解析错误也按同一参数重发并在第三次失败后抛出。

    参数：无

    返回：
        None，通过断言验证解析错误重试次数和参数一致性
    """
    inner = _ScriptedProvider([LLMParseError("坏 JSON")] * 3)
    with pytest.raises(LLMParseError, match="坏 JSON"):
        await _wrap(inner).chat("sys", _MESSAGES, _TOOLS)
    assert len(inner.calls) == 3
    assert inner.calls[0] == inner.calls[1] == inner.calls[2]


async def test_retry_preserves_every_failed_response_raw() -> None:
    """验证重试成功或耗尽时都保留每次已收到但解析失败的原始响应。

    参数：无

    返回：
        None，断言成功响应和最终异常的 raw 均按尝试顺序包含失败原文
    """
    first = LLMParseError("坏 JSON", raw="raw-bad-1")
    success = await _wrap(_ScriptedProvider([first, _OK])).chat("sys", _MESSAGES, _TOOLS)
    assert success.raw == "raw-bad-1\nraw-ok"

    errors = [LLMParseError(f"坏 JSON {i}", raw=f"raw-bad-{i}") for i in range(1, 4)]
    with pytest.raises(LLMParseError) as caught:
        await _wrap(_ScriptedProvider(errors)).chat("sys", _MESSAGES, _TOOLS)
    assert caught.value.raw == "raw-bad-1\nraw-bad-2\nraw-bad-3"


async def test_unexpected_exception_also_retried() -> None:
    """验证非模型异常同样参与重试且第三次成功时正常放行。

    参数：无

    返回：
        None，通过断言验证最终响应和三次调用记录
    """
    inner = _ScriptedProvider([ValueError("意外"), ValueError("意外"), _OK])
    resp = await _wrap(inner).chat("sys", _MESSAGES, _TOOLS)
    assert resp is _OK
    assert len(inner.calls) == 3


async def test_cancelled_error_propagates_without_retry() -> None:
    """验证任务取消异常不被重试包装器吞掉或重复执行。

    参数：无

    返回：
        None，通过断言验证 CancelledError 直接传播且仅调用一次
    """
    inner = _ScriptedProvider([asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await _wrap(inner).chat("sys", _MESSAGES, _TOOLS)
    assert len(inner.calls) == 1


async def test_first_success_no_retry() -> None:
    """验证首次调用成功时立即返回且不会产生多余重试。

    参数：无

    返回：
        None，通过断言验证返回对象与一次调用记录
    """
    inner = _ScriptedProvider([_OK])
    resp = await _wrap(inner).chat("sys", _MESSAGES, _TOOLS)
    assert resp is _OK
    assert len(inner.calls) == 1


def test_tool_result_message_delegates() -> None:
    """验证工具结果消息的构造直接委托给底层 provider。

    参数：无

    返回：
        None，通过断言验证包装器保留底层消息格式
    """
    inner = _ScriptedProvider([])
    msg = _wrap(inner).tool_result_message(ToolCall(name="t", args={}, call_id="c1"), "结果")
    assert msg == {"role": "tool", "tool_call_id": "c1", "content": "结果"}


# ---------- backoff 退避 ----------


def test_delay_uses_backoff_steps_then_last() -> None:
    """验证退避延迟按档位选择、越界沿用末档且空配置返回零。

    参数：无

    返回：
        None，通过断言验证首档、末档、越界与空档位结果
    """
    provider = RetryingProvider(_ScriptedProvider([]), backoff=(1.0, 3.0))
    assert provider._delay(0) == 1.0
    assert provider._delay(1) == 3.0
    assert provider._delay(7) == 3.0  # 末档沿用
    assert RetryingProvider(_ScriptedProvider([]), backoff=())._delay(0) == 0.0


async def test_backoff_sleep_between_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证默认退避在相邻尝试之间依次请求等待一秒和三秒。

    参数：
        monkeypatch: pytest.MonkeyPatch，用于把 asyncio.sleep 替换为记录桩

    返回：
        None，通过断言验证成功响应和两次退避延迟
    """
    slept: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        """记录包装器请求的退避时长而不进行真实等待。

        参数：
            delay: float，本次请求等待的秒数

        返回：
            None，副作用为把时长追加到 slept(等待记录)列表
        """
        slept.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    inner = _ScriptedProvider([LLMError("x"), LLMError("y"), _OK])
    resp = await RetryingProvider(inner).chat("sys", _MESSAGES, _TOOLS)  # 默认 backoff
    assert resp is _OK
    assert slept == [1.0, 3.0]


# ---------- factory 接线守卫（三 agent 重试覆盖的根基） ----------


@pytest.mark.parametrize(
    ("provider_name", "inner_cls"),
    [
        ("anthropic", AnthropicProvider),
        ("openai_compat", OpenAICompatProvider),
        ("openai_responses", OpenAIResponsesProvider),
    ],
)
def test_create_provider_wraps_all_real_providers(
    monkeypatch: pytest.MonkeyPatch, provider_name: str, inner_cls: type
) -> None:
    """验证工厂创建的三个真实 provider 都统一包裹同参重试层。

    参数：
        monkeypatch: pytest.MonkeyPatch，用于注入测试 API key
        provider_name: str，参数化传入的 provider 配置名称
        inner_cls: type，期望被包装的底层 provider 类型

    返回：
        None，通过断言验证外层与内层 provider 类型
    """
    monkeypatch.setenv("LLM_KEY_T", "sk-test")
    cred = CredentialConfig(name="t", provider=provider_name, model="m")  # type: ignore[arg-type]
    provider = create_provider(cred)
    assert isinstance(provider, RetryingProvider)
    assert isinstance(provider._inner, inner_cls)
