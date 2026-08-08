"""LLM 调用重试装饰器：chat 失败以相同参数重发，兜住网络抖动与偶发坏输出。

语义：
- chat 抛任何 Exception（LLMError 传输/API 错误、LLMParseError 输出解析失败、
  意外异常）→ 以完全相同的 system/messages/tools 重发，最多 max_attempts 次；
  只读重发，不修改调用方 messages
- 全部失败抛最后一个异常（原类型原消息）：上层失败口径（error 落库、连续失败
  计数、kill_switch）与无重试时完全一致
- 不捕 BaseException：asyncio.CancelledError（研报超时保险丝）等直接传播
- 偶发坏 JSON 依赖采样非确定性自愈（默认温度非 0）；系统性格式错误重试耗尽后
  按现有 error 口径落库，作为人工调整 prompt 的信号
"""

from __future__ import annotations

import asyncio

from src.agent.providers.base import LLMProvider, LLMResponse, ToolCall
from src.audit.logger import get_logger

logger = get_logger(__name__)


class RetryingProvider:
    """LLMProvider 同参重试装饰器（factory 统一包裹真实 provider）。"""

    def __init__(
        self,
        inner: LLMProvider,
        max_attempts: int = 3,
        backoff: tuple[float, ...] = (1.0, 3.0),
    ) -> None:
        self._inner = inner
        self._max_attempts = max(1, max_attempts)
        self._backoff = backoff

    async def chat(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        """调用 inner.chat；失败同参重发（间隔 backoff），耗尽后抛最后一个异常。"""
        last: Exception | None = None
        for attempt in range(self._max_attempts):
            try:
                return await self._inner.chat(system, messages, tools)
            except Exception as exc:
                last = exc
                if attempt >= self._max_attempts - 1:
                    break
                delay = self._delay(attempt)
                logger.warning(
                    "LLM 调用失败（第 %d/%d 次），%.1f 秒后同参重试：%s: %s",
                    attempt + 1,
                    self._max_attempts,
                    delay,
                    type(exc).__name__,
                    exc,
                )
                await asyncio.sleep(delay)
        assert last is not None  # max_attempts ≥ 1，循环至少执行一次
        raise last

    def _delay(self, attempt: int) -> float:
        """第 attempt 次（0 起）失败后的等待秒数；backoff 不足时沿用末档，空配置为 0。"""
        if not self._backoff:
            return 0.0
        return self._backoff[min(attempt, len(self._backoff) - 1)]

    def tool_result_message(self, call: ToolCall, result: str) -> dict:
        """透传工具结果包装（无状态，直接委托 inner）。"""
        return self._inner.tool_result_message(call, result)
