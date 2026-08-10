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
        """初始化重试装饰器，保存被包裹的 provider 与重试参数。

        参数：
            inner: LLMProvider，被包裹的真实 provider，所有调用最终委托给它
            max_attempts: int，单次 chat 的最大尝试次数（含首次），小于 1 时按 1 处理
            backoff: tuple[float, ...]，第 i 次失败后的等待秒数表；次数超出表长时沿用
                末档，省略时默认 (1.0, 3.0)，传空元组表示失败后立即重试不等候

        返回：
            None，仅初始化实例状态（保存 inner、钳位后的 max_attempts 与 backoff）
        """
        self._inner = inner
        self._max_attempts = max(1, max_attempts)
        self._backoff = backoff

    async def chat(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        """调用底层 LLM 提供器，并在普通异常时按退避配置使用原参数重试。

        参数：
            system: str，系统提示词
            messages: list[dict]，当前对话消息列表
            tools: list[dict]，可供模型调用的工具定义

        返回：
            LLMResponse，首次成功尝试得到的统一模型响应

        异常：
            Exception: 所有尝试均失败时重新抛出最后一次底层异常
        """
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
        """计算指定失败次数后的等待时长，退避表不足时沿用最后一档。

        参数：
            attempt: int，从零开始的失败次数索引

        返回：
            float，本次重试前应等待的秒数；空退避配置返回 0
        """
        if not self._backoff:
            return 0.0
        return self._backoff[min(attempt, len(self._backoff) - 1)]

    def tool_result_message(self, call: ToolCall, result: str) -> dict:
        """委托底层提供器把工具执行结果包装为其原生消息格式。

        参数：
            call: ToolCall，触发本次执行的工具调用
            result: str，工具执行结果文本

        返回：
            dict，可追加到对话上下文的厂商原生工具结果消息
        """
        return self._inner.tool_result_message(call, result)
