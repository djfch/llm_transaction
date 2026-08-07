"""研报专用确定性 Mock LLM Provider：冒烟/集成测试用。

行为：第一轮返回研报工具调用（fetch_calendar + fetch_indicators），
第二轮返回合法研报 JSON —— 保证 mock 模式下研报链路产出确定性的成功结果
（交易 agent 的 MockProvider 返回交易工具与非 JSON 文本，不适用于研报）。
"""

from __future__ import annotations

import json

from src.agent.providers.base import LLMResponse, ToolCall

_REPORT_JSON = json.dumps(
    {
        "direction": "中性",
        "confidence": "中",
        "horizon": "当日",
        "evidence": [{"point": "mock 验证数据", "source": "mock"}],
        "risks": ["mock 模式无真实风险判断"],
        "narrative": "mock 研报：链路验证用，无真实判断。",
    },
    ensure_ascii=False,
)


class ResearchMockProvider:
    """实现 LLMProvider 协议的研报专用 Mock（不触网，输出确定性成功）。"""

    def __init__(self) -> None:
        self._calls = 0

    async def chat(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        self._calls += 1
        if self._calls == 1:
            return LLMResponse(
                text="先看日历与指标快照",
                tool_calls=[
                    ToolCall("fetch_calendar", {}),
                    ToolCall("fetch_indicators", {}),
                ],
                raw="research-mock-raw-round-1",
                assistant_message={"role": "assistant", "content": "mock 工具调用轮"},
            )
        return LLMResponse(
            text=_REPORT_JSON,
            raw=f"research-mock-raw-{self._calls}",
            assistant_message={"role": "assistant", "content": _REPORT_JSON},
        )

    def tool_result_message(self, call: ToolCall, result: str) -> dict:
        return {"role": "user", "content": f"工具 {call.name} 执行结果：{result}"}
