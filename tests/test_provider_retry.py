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
        self._script = list(script)
        self.calls: list[tuple[str, list[dict], list[dict]]] = []

    async def chat(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        self.calls.append((system, copy.deepcopy(messages), copy.deepcopy(tools)))
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def tool_result_message(self, call: ToolCall, result: str) -> dict:
        return {"role": "tool", "tool_call_id": call.call_id, "content": result}


def _wrap(inner: _ScriptedProvider) -> RetryingProvider:
    """统一零退避，测试不等 sleep。"""
    return RetryingProvider(inner, backoff=(0, 0))


async def test_success_after_two_transport_errors() -> None:
    """两次网络/API 错误后第 3 次成功：返回结果，三次收到的参数完全相同。"""
    inner = _ScriptedProvider([LLMError("连接超时"), LLMError("502"), _OK])
    resp = await _wrap(inner).chat("sys", _MESSAGES, _TOOLS)
    assert resp is _OK
    assert len(inner.calls) == 3
    assert inner.calls[0] == inner.calls[1] == inner.calls[2] == ("sys", _MESSAGES, _TOOLS)


async def test_caller_arguments_not_mutated() -> None:
    """重试不修改调用方传入的 messages/tools。"""
    messages = [{"role": "user", "content": "原始"}]
    tools = [{"name": "t", "description": "d", "parameters": {}}]
    inner = _ScriptedProvider([LLMError("x"), _OK])
    await _wrap(inner).chat("sys", messages, tools)
    assert messages == [{"role": "user", "content": "原始"}]
    assert tools == [{"name": "t", "description": "d", "parameters": {}}]


async def test_three_llm_errors_raise_original() -> None:
    """连续 3 次 LLMError：耗尽后抛原类型原消息。"""
    inner = _ScriptedProvider([LLMError("e1"), LLMError("e2"), LLMError("e3")])
    with pytest.raises(LLMError, match="e3"):
        await _wrap(inner).chat("sys", _MESSAGES, _TOOLS)
    assert len(inner.calls) == 3


async def test_parse_error_retried_same_params_then_raise() -> None:
    """LLMParseError 同样同参重发（无特殊反馈路径），3 次后抛。"""
    inner = _ScriptedProvider([LLMParseError("坏 JSON")] * 3)
    with pytest.raises(LLMParseError, match="坏 JSON"):
        await _wrap(inner).chat("sys", _MESSAGES, _TOOLS)
    assert len(inner.calls) == 3
    assert inner.calls[0] == inner.calls[1] == inner.calls[2]


async def test_unexpected_exception_also_retried() -> None:
    """非 LLMError 的意外异常同样重试，第 3 次成功则放行。"""
    inner = _ScriptedProvider([ValueError("意外"), ValueError("意外"), _OK])
    resp = await _wrap(inner).chat("sys", _MESSAGES, _TOOLS)
    assert resp is _OK
    assert len(inner.calls) == 3


async def test_cancelled_error_propagates_without_retry() -> None:
    """CancelledError（BaseException）不重试、直接传播。"""
    inner = _ScriptedProvider([asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await _wrap(inner).chat("sys", _MESSAGES, _TOOLS)
    assert len(inner.calls) == 1


async def test_first_success_no_retry() -> None:
    """一次成功：inner 只被调一次。"""
    inner = _ScriptedProvider([_OK])
    resp = await _wrap(inner).chat("sys", _MESSAGES, _TOOLS)
    assert resp is _OK
    assert len(inner.calls) == 1


def test_tool_result_message_delegates() -> None:
    """tool_result_message 直接委托 inner。"""
    inner = _ScriptedProvider([])
    msg = _wrap(inner).tool_result_message(ToolCall(name="t", args={}, call_id="c1"), "结果")
    assert msg == {"role": "tool", "tool_call_id": "c1", "content": "结果"}


# ---------- backoff 退避 ----------


def test_delay_uses_backoff_steps_then_last() -> None:
    """_delay 按档位取值，超出后沿用末档；空配置恒 0。"""
    provider = RetryingProvider(_ScriptedProvider([]), backoff=(1.0, 3.0))
    assert provider._delay(0) == 1.0
    assert provider._delay(1) == 3.0
    assert provider._delay(7) == 3.0  # 末档沿用
    assert RetryingProvider(_ScriptedProvider([]), backoff=())._delay(0) == 0.0


async def test_backoff_sleep_between_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认退避档真实生效：两次失败分别 sleep 1s、3s。"""
    slept: list[float] = []

    async def _fake_sleep(delay: float) -> None:
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
    """factory 三个真实分支全部外裹 RetryingProvider；任一被改回裸 provider 即红。"""
    monkeypatch.setenv("LLM_KEY_T", "sk-test")
    cred = CredentialConfig(name="t", provider=provider_name, model="m")  # type: ignore[arg-type]
    provider = create_provider(cred)
    assert isinstance(provider, RetryingProvider)
    assert isinstance(provider._inner, inner_cls)
