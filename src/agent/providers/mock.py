"""确定性 Mock LLM Provider：冒烟测试与集成测试用。

行为：第一次 chat 返回三个工具调用（查账户/写笔记/设唤醒），
之后每轮返回纯文本"观望不交易"，保证链路可重复验证。
"""

from __future__ import annotations

from src.agent.providers.base import LLMResponse, ToolCall


class MockProvider:
    """实现 LLMProvider 协议的假 Provider（不触网）。"""

    def __init__(self) -> None:
        """初始化假 Provider，把对话轮次计数器清零。

        参数：无

        返回：None，就地初始化实例的轮次计数器
        """
        self._calls = 0

    async def chat(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        """按预设剧本返回确定性的假 LLM 回复（不触网）。

        首轮返回三个工具调用（查行情/写笔记/设唤醒），后续每轮返回纯文本"观望不交易"，
        保证冒烟与集成测试链路可重复验证。

        参数：
            system: str，系统提示词（本实现忽略，仅为满足协议签名）
            messages: list[dict]，历史对话消息（本实现忽略，仅为满足协议签名）
            tools: list[dict]，可用工具定义（本实现忽略，仅为满足协议签名）

        返回：
            LLMResponse：首轮含三个工具调用的回复，或后续轮的纯文本观望回复
        """
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
        """把一次工具执行结果包装成 user 角色消息，供追加进 messages 继续对话。

        参数：
            call: ToolCall，已执行的工具调用（取其工具名）
            result: str，工具执行结果的文本

        返回：
            dict：{"role": "user", "content": ...} 形式的消息字典
        """
        return {"role": "user", "content": f"工具 {call.name} 执行结果：{result}"}
