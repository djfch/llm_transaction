"""研报专用确定性 Mock LLM Provider：冒烟/集成测试用。

行为：每轮根据冻结白名单返回基础工具与逐标的市场工具调用，
得到工具结果后返回合法 v2 研报 JSON —— 保证 mock 模式下研报链路产出确定性的成功结果
（交易 agent 的 MockProvider 返回交易工具与非 JSON 文本，不适用于研报）。
"""

from __future__ import annotations

import json

from src.agent.providers.base import LLMResponse, ToolCall


def _watchlist_contracts(messages: list[dict]) -> tuple[str, ...]:
    for message in messages:
        content = message.get("content")
        if not isinstance(content, str) or "## 本轮白名单" not in content:
            continue
        section = content.split("## 本轮白名单", 1)[1]
        contracts: list[str] = []
        for line in section.splitlines():
            value = line.strip()
            if value.startswith("## ") or value.startswith("必须"):
                break
            if value.startswith("- "):
                contracts.append(value[2:].strip())
        return tuple(dict.fromkeys(contract for contract in contracts if contract))
    return ()


def _report_json(contracts: tuple[str, ...]) -> str:
    asset_views = [
        {
            "contract": contract,
            "direction": "中性",
            "confidence": "低",
            "horizon": "当日",
            "market_regime": "震荡",
            "technical_confirmation": "不可用",
            "basis_type": "结构延续",
            "evidence": [{"point": "mock 验证数据", "source": "mock"}],
            "risks": ["mock 模式无真实风险判断"],
            "narrative": "mock 研报：链路验证用，无真实判断。",
        }
        for contract in contracts
    ]
    return json.dumps(
        {
            "schema_version": 2,
            "summary": "mock 研报总览",
            "cross_market_view": "mock 跨标的观察",
            "global_risks": ["mock 模式不代表真实行情"],
            "asset_views": asset_views,
        },
        ensure_ascii=False,
    )


class ResearchMockProvider:
    """实现 LLMProvider 协议的研报专用 Mock（不触网，输出确定性成功）。"""

    def __init__(self) -> None:
        self._calls = 0

    async def chat(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        self._calls += 1
        contracts = _watchlist_contracts(messages)
        has_market_result = any(
            isinstance(message.get("content"), str)
            and "工具 get_research_market_data 执行结果" in message["content"]
            for message in messages
        )
        if contracts and not has_market_result:
            return LLMResponse(
                text="先看日历、指标与逐标的市场快照",
                tool_calls=[
                    ToolCall("fetch_calendar", {}),
                    ToolCall("fetch_indicators", {}),
                    *[
                        ToolCall(
                            "get_research_market_data",
                            {"contract": contract, "limit": 30},
                        )
                        for contract in contracts
                    ],
                ],
                raw="research-mock-raw-round-1",
                assistant_message={"role": "assistant", "content": "mock 工具调用轮"},
            )
        report_json = _report_json(contracts)
        return LLMResponse(
            text=report_json,
            raw=f"research-mock-raw-{self._calls}",
            assistant_message={"role": "assistant", "content": report_json},
        )

    def tool_result_message(self, call: ToolCall, result: str) -> dict:
        return {"role": "user", "content": f"工具 {call.name} 执行结果：{result}"}
