"""研报专用确定性 Mock LLM Provider：冒烟/集成测试用。

行为：每轮根据冻结白名单返回基础工具与逐标的市场工具调用，
得到工具结果后返回合法 v2 研报 JSON —— 保证 mock 模式下研报链路产出确定性的成功结果
（交易 agent 的 MockProvider 返回交易工具与非 JSON 文本，不适用于研报）。
"""

from __future__ import annotations

import json

from src.agent.providers.base import LLMResponse, ToolCall
from src.utils import LLMIdentity


def _watchlist_contracts(messages: list[dict]) -> tuple[str, ...]:
    """从对话消息中解析「本轮白名单」段落，提取本轮研报覆盖的合约列表。

    参数：
        messages: list[dict]，多轮对话消息列表，在其中查找含「## 本轮白名单」段落的消息

    返回：
        tuple[str, ...]：按出现顺序去重后的合约名元组；未找到白名单段落时返回空元组
    """
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
    """按给定合约列表拼出确定性的 v2 研报 JSON 字符串（mock 数据，仅供链路验证）。

    参数：
        contracts: tuple[str, ...]，本轮白名单合约名，逐个生成一条中性立场的标的观点

    返回：
        str：schema_version=3 的研报 JSON 字符串，各字段均为固定 mock 内容
    """
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
            "schema_version": 3,
            "summary": "mock 研报总览",
            "cross_market_view": "mock 跨标的观察",
            "global_risks": ["mock 模式不代表真实行情"],
            "asset_views": asset_views,
        },
        ensure_ascii=False,
    )


class ResearchMockProvider:
    """实现 LLMProvider 协议的研报专用 Mock（不触网，输出确定性成功）。"""

    def __init__(self, identity: LLMIdentity | None = None) -> None:
        """初始化 Mock Provider，对话轮次计数器归零。

        参数：
            identity: LLMIdentity | None，注入的模型身份；None 时取全空默认值（与历史轮同口径）

        返回：None，仅初始化内部状态（轮次计数器置 0、模型身份落为注入值或全空默认值）
        """
        self._calls = 0
        self.identity = identity if identity is not None else LLMIdentity()

    async def chat(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        """模拟一轮研报对话：首轮按白名单发出工具调用，拿到市场数据后返回 v2 研报 JSON。

        参数：
            system: str，系统提示词（mock 不读取，仅为保持接口一致）
            messages: list[dict]，多轮对话消息，用于解析白名单及判断市场工具结果是否已回填
            tools: list[dict]，可用工具定义（mock 不读取，仅为保持接口一致）

        返回：
            LLMResponse：白名单非空且尚无市场工具结果时，返回 fetch_calendar、
            fetch_indicators 与逐标的 get_research_market_data 的工具调用；
            否则返回确定性的 v2 研报 JSON 文本
        """
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
        """把一次工具执行结果包装成 user 角色消息，供回填对话继续下一轮。

        参数：
            call: ToolCall，已执行的工具调用，取其中的工具名写入消息文本
            result: str，工具执行结果文本

        返回：
            dict：形如 {"role": "user", "content": "工具 <工具名> 执行结果：<结果>"} 的消息
        """
        return {"role": "user", "content": f"工具 {call.name} 执行结果：{result}"}
