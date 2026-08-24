"""成交来源（trades.source）标注测试：paper drain 三态。

当前 agent 决策层口径：
- paper 模式决策循环 drain 落库时按 FillRecord 标注：强平（order_id=="liquidation"）
  → liquidation；fill.is_close → llm_close；其余 → llm_open
- 真实网关 trades 由 ExchangeFillSync 按交易所成交回报分类落库（见 test_agent_fill_sync.py）
- user_close 由 DecisionLoop.manual_close 标注（见 test_agent_manual_close.py）
"""

from __future__ import annotations

import time
from collections import deque
from decimal import Decimal
from types import SimpleNamespace

from src.agent import DecisionLoop, LLMResponse, PromptLoader, ToolCall
from src.config import AuditConfig, PaperConfig, Settings
from src.gateway.base import Contract
from src.market.candles import CandleCache, ManualPriceSource
from src.market.triggers import TriggerManager
from src.memory import Database, Repo
from src.paper.engine import PaperGateway
from src.risk.engine import RiskEngine


class SeqProvider:
    """预置响应序列的 mock provider；元素为异常则抛出。"""

    def __init__(self, responses: list) -> None:
        """保存预置响应序列，供 chat 按调用顺序依次消费。

        参数：
            responses: list，预置的 LLM 响应或异常实例列表，按顺序逐次弹出

        返回：
            None，就地初始化内部响应队列
        """
        self._responses = deque(responses)

    async def chat(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        """按预置顺序返回响应，并把队列中的异常原样抛出。

        参数：
            system: str，系统提示词，本桩不解析其内容
            messages: list[dict]，对话消息列表，本桩不解析其内容
            tools: list[dict]，可调用工具定义，本桩不解析其内容

        返回：
            LLMResponse，队首预置响应；队列为空时返回兜底文本

        异常：
            Exception: 当队首预置元素是异常实例时原样抛出
        """
        if not self._responses:
            return LLMResponse(text="（无更多预置响应）", raw="{}")
        item = self._responses.popleft()
        if isinstance(item, Exception):
            raise item
        return item

    def tool_result_message(self, call: ToolCall, result: str) -> dict:
        """把工具执行结果包装为 OpenAI 风格的工具消息。

        参数：
            call: ToolCall，包含调用编号的工具调用
            result: str，工具执行后返回的文本

        返回：
            dict，关联原工具调用编号的消息字典
        """
        return {"role": "tool", "tool_call_id": call.call_id, "content": result}


def _resp(text: str, calls: list[ToolCall], raw: str = "{}") -> LLMResponse:
    """构造带可选工具调用的测试用 LLM 响应。

    参数：
        text: str，助手回复正文
        calls: list[ToolCall]，本轮提出的工具调用列表
        raw: str，原始响应文本，默认空 JSON

    返回：
        LLMResponse，字段完整的模拟模型响应
    """
    return LLMResponse(
        text=text,
        tool_calls=calls,
        raw=raw,
        assistant_message={"role": "assistant", "content": text or "（调用工具）"},
    )


def _contract(name: str, quanto: str, mark: str) -> Contract:
    """构造指定名称、合约乘数和标记价的测试合约。

    参数：
        name: str，永续合约名称
        quanto: str，合约乘数的十进制字符串
        mark: str，标记价的十进制字符串

    返回：
        Contract，具有固定手续费和交易边界的合约元数据
    """
    return Contract(
        name=name,
        quanto_multiplier=Decimal(quanto),
        order_size_min=Decimal(1),
        order_size_max=Decimal("1000000"),
        order_price_round=Decimal("0.1"),
        enable_decimal=False,
        mark_price=Decimal(mark),
        funding_rate=Decimal("0.0001"),
        funding_interval=28800,
        maker_fee_rate=Decimal("0.0002"),
        taker_fee_rate=Decimal("0.0005"),
        status="trading",
        in_delisting=False,
    )


async def _make_paper_loop(tmp_path, provider: SeqProvider) -> SimpleNamespace:
    """组装包含模拟撮合与成交排空链路的 paper 决策循环测试环境。

    参数：
        tmp_path: Path，pytest 临时目录，用于隔离数据库、审计与提示词文件
        provider: SeqProvider，按序提供决策响应的模拟模型

    返回：
        SimpleNamespace，包含 db(数据库)、repo(仓储)、gateway(paper 网关)和 loop(决策循环)
    """
    db = Database()
    await db.open(tmp_path / "agent.db")
    repo = Repo(db)
    gateway = PaperGateway(PaperConfig(initial_equity=Decimal("10000")))
    gateway.upsert_contract(_contract("BTC_USDT", "0.001", "60000"))
    gateway.on_price("BTC_USDT", Decimal("60000"))
    prompt_path = tmp_path / "system_prompt.md"
    prompt_path.write_text("# 策略书\n稳健交易，控制回撤。", encoding="utf-8")
    loop = DecisionLoop(
        settings=Settings(audit=AuditConfig(dir=str(tmp_path / "audit"))),
        watchlist=["BTC_USDT"],
        provider=provider,
        gateway=gateway,
        risk_engine=RiskEngine(),
        repo=repo,
        candles=CandleCache(gateway, ManualPriceSource()),
        triggers=TriggerManager(lambda t, p: None),
        prompt_loader=PromptLoader(prompt_path),
        drain_fills=gateway.drain_fills,
    )
    return SimpleNamespace(db=db, repo=repo, gateway=gateway, loop=loop)


# ---------- drain 落库 source 三态：llm_open / llm_close / liquidation ----------


async def test_drain_trade_source_open_close_liquidation(tmp_path):
    """验证 paper 成交排空时按开仓、平仓与强平三种来源准确落库。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证四笔成交的 source(成交来源)顺序
    """
    provider = SeqProvider(
        [
            _resp(
                "开多",
                [
                    ToolCall(
                        "place_order",
                        {
                            "contract": "BTC_USDT",
                            "side": "long",
                            "margin_usdt": 60,
                            "leverage": 1,
                            "stop_loss_price": 58000,
                        },
                        "c1",
                    )
                ],
            ),
            _resp("好", []),  # round1：drain 落 llm_open
            _resp("平仓", [ToolCall("place_order", {"contract": "BTC_USDT", "close": True}, "c2")]),
            _resp("好", []),  # round2：drain 落 llm_close
            _resp(
                "再开",
                [
                    ToolCall(
                        "place_order",
                        {
                            "contract": "BTC_USDT",
                            "side": "long",
                            "margin_usdt": 12,
                            "leverage": 5,
                            "stop_loss_price": 1,
                        },
                        "c3",
                    )
                ],
            ),
            _resp("好", []),  # round3：drain 落 llm_open（5x 杠杆仓）
            _resp("观望", []),  # round4：drain 落强平成交
        ]
    )
    env = await _make_paper_loop(tmp_path, provider)
    try:
        for _ in range(3):
            assert (await env.loop.run_once("timer")).ok
        # 5x 杠杆多仓（保证金 12），标记价崩到 40000：保证金率转负 → 触发强平
        env.gateway.on_price("BTC_USDT", Decimal("40000"))
        assert len(env.gateway.liquidations) == 1
        assert (await env.loop.run_once("timer")).ok

        trades = await env.repo.trades_between(0.0, time.time() + 1)
        assert [t.source for t in trades] == [
            "llm_open",
            "llm_close",
            "llm_open",
            "liquidation",
        ]
    finally:
        await env.db.close()
