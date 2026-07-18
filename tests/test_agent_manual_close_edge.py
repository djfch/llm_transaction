"""manual_close paper 路径边界回归（对抗性审查必修项）。

覆盖：
- paper 无持仓平仓：文本「当前无持仓」，不落 trades 行
- 平仓成交已生效但本地落库失败：响应 text 回填警告（不静默吞没），与 orders 路径对齐
- 直接消费成交缓冲：缓冲中夹带的 fill（LLM 开仓/强平）按标准标注落库，
  本单反写 user_close，轮末 drain 不再双计
"""

from __future__ import annotations

import time
from collections import deque
from decimal import Decimal
from types import SimpleNamespace

from src.agent import DecisionLoop, LLMResponse, PromptLoader, ToolCall
from src.config import AuditConfig, PaperConfig, Settings
from src.gateway.base import Contract, OrderRequest
from src.market.candles import CandleCache, ManualPriceSource
from src.market.triggers import TriggerManager
from src.memory import Database, Repo
from src.paper.engine import PaperGateway
from src.risk.engine import RiskEngine


class SeqProvider:
    """预置响应序列的 mock provider；元素为异常则抛出。"""

    def __init__(self, responses: list) -> None:
        self._responses = deque(responses)

    async def chat(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        if not self._responses:
            return LLMResponse(text="（无更多预置响应）", raw="{}")
        item = self._responses.popleft()
        if isinstance(item, Exception):
            raise item
        return item

    def tool_result_message(self, call: ToolCall, result: str) -> dict:
        return {"role": "tool", "tool_call_id": call.call_id, "content": result}


_CONTRACTS = {
    "BTC_USDT": ("0.001", "60000"),
    "ETH_USDT": ("0.01", "3000"),
}


def _contract(name: str) -> Contract:
    quanto, mark = _CONTRACTS[name]
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


async def _make_paper_loop(tmp_path, *contracts: str) -> SimpleNamespace:
    """paper 决策循环（PaperGateway + drain_fills），审计快照隔离到 tmp_path。"""
    db = Database()
    await db.open(tmp_path / "agent.db")
    repo = Repo(db)
    gateway = PaperGateway(PaperConfig(initial_equity=Decimal("10000")))
    for name in contracts:
        gateway.upsert_contract(_contract(name))
        gateway.on_price(name, _contract(name).mark_price)
    prompt_path = tmp_path / "system_prompt.md"
    prompt_path.write_text("# 策略书\n稳健交易，控制回撤。", encoding="utf-8")
    loop = DecisionLoop(
        settings=Settings(audit=AuditConfig(dir=str(tmp_path / "audit"))),
        watchlist=list(contracts),
        provider=SeqProvider([]),
        gateway=gateway,
        risk_engine=RiskEngine(),
        repo=repo,
        candles=CandleCache(gateway, ManualPriceSource()),
        triggers=TriggerManager(lambda t, p: None),
        prompt_loader=PromptLoader(prompt_path),
        drain_fills=gateway.drain_fills,
    )
    return SimpleNamespace(db=db, repo=repo, gateway=gateway, loop=loop)


# ---------- paper 无持仓平仓：文本诚实、零成交行 ----------


async def test_manual_close_paper_no_position(tmp_path):
    env = await _make_paper_loop(tmp_path, "BTC_USDT")
    try:
        result = await env.loop.manual_close("BTC_USDT")
        assert "无持仓" in result["text"]
        assert await env.repo.trades_between(0.0, time.time() + 1) == []
    finally:
        await env.db.close()


# ---------- 平仓成交已生效但本地落库失败：响应回填警告（不静默） ----------


async def test_manual_close_trade_persist_failure_warns(tmp_path, monkeypatch):
    env = await _make_paper_loop(tmp_path, "BTC_USDT")
    try:
        env.gateway.place_order(OrderRequest(contract="BTC_USDT", size=Decimal(1)))

        async def _boom(**kwargs):
            raise RuntimeError("db down")

        monkeypatch.setattr(env.repo, "save_trade", _boom)
        result = await env.loop.manual_close("BTC_USDT")

        assert result["status"] == "finished"  # 成交在网关账本已生效
        assert "本地记录失败" in result["text"]  # 警告回填（对齐 orders 路径语义）
        assert await env.repo.trades_between(0.0, time.time() + 1) == []
    finally:
        await env.db.close()


# ---------- 直接消费成交缓冲：夹带 fill 标准标注、本单反写 user_close、drain 不双计 ----------


async def test_manual_close_flushes_buffer_with_source_labels(tmp_path):
    env = await _make_paper_loop(tmp_path, "BTC_USDT", "ETH_USDT")
    try:
        gateway = env.gateway
        gateway.set_leverage("BTC_USDT", 5)
        gateway.place_order(OrderRequest(contract="BTC_USDT", size=Decimal(1)))  # 5x 多仓
        gateway.place_order(OrderRequest(contract="ETH_USDT", size=Decimal(1)))  # ETH 多仓
        # BTC 崩盘触发强平：强平 fill 入缓冲但尚未落库（模拟轮中/轮间夹带）
        gateway.on_price("BTC_USDT", Decimal("40000"))
        assert len(gateway.liquidations) == 1

        result = await env.loop.manual_close("ETH_USDT")
        assert result["status"] == "finished"

        trades = await env.repo.trades_between(0.0, time.time() + 1)
        assert [(t.contract, t.source) for t in trades] == [
            ("BTC_USDT", "llm_open"),
            ("ETH_USDT", "llm_open"),
            ("BTC_USDT", "liquidation"),  # 夹带强平按标准标注
            ("ETH_USDT", "user_close"),
        ]
        assert (await env.loop.run_once("timer")).ok  # 缓冲已消费，drain 无货 → 不双计
        assert len(await env.repo.trades_between(0.0, time.time() + 1)) == 4
    finally:
        await env.db.close()
