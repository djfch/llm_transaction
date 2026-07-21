"""确定性 Mock LLM Provider：冒烟测试与集成测试用。

行为：第一次 chat 返回三个工具调用（查账户/写笔记/设唤醒），
之后每轮返回纯文本"观望不交易"，保证链路可重复验证。
"""

from __future__ import annotations

from src.agent.providers.base import LLMResponse, ToolCall


class MockProvider:
    """实现 LLMProvider 协议的假 Provider（不触网）。"""

    def __init__(self) -> None:
        self._calls = 0

    async def chat(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        self._calls += 1
        if self._calls == 1:
            return LLMResponse(
                text="首次分析：账户上下文已注入，查询行情、记录笔记并设置唤醒",
                tool_calls=[
                    ToolCall("get_market_data", {"contract": "BTC_USDT", "interval": "1h"}),
                    ToolCall("write_note", {"content": "mock 首轮：行情不明，保持观望"}),
                    ToolCall("set_next_wakeup", {"minutes": 5}),
                ],
                raw="mock-raw-round-1",
                assistant_message={"role": "assistant", "content": "mock 工具调用轮"},
            )
        return LLMResponse(
            text="本轮观望，不交易",
            raw=f"mock-raw-{self._calls}",
            assistant_message={"role": "assistant", "content": "mock 观望"},
        )

    def tool_result_message(self, call: ToolCall, result: str) -> dict:
        return {"role": "user", "content": f"工具 {call.name} 执行结果：{result}"}
