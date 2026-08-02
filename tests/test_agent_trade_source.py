"""成交来源（trades.source）标注测试：drain 三态 + 真实网关 inline 两态。

当前 agent 决策层口径：
- paper 模式决策循环 drain 落库时按 FillRecord 标注：强平（order_id=="liquidation"）
  → liquidation；fill.is_close → llm_close；其余 → llm_open
- 真实网关（无 drain 钩子）工具层 inline 落库按请求标注：close/reduce_only → llm_close，
  否则 → llm_open
- user_close 由 DecisionLoop.manual_close 标注（见 test_agent_manual_close.py）
"""

from __future__ import annotations

import time
from collections import deque
from decimal import Decimal
from types import SimpleNamespace

from src.agent import DecisionLoop, LLMResponse, PromptLoader, ToolCall
from src.agent.tool_handlers import ToolDeps
from src.agent.tools import ToolRegistry
from src.config import AuditConfig, PaperConfig, RiskConfig, Settings
from src.gateway.base import Contract
from src.gateway.mock import MockGateway
from src.market.candles import CandleCache, ManualPriceSource
from src.market.triggers import TriggerManager
from src.memory import Database, Repo
from src.paper.engine import PaperGateway
from src.risk.engine import RiskEngine
from src.risk.models import DailyStats


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


def _resp(text: str, calls: list[ToolCall], raw: str = "{}") -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=calls,
        raw=raw,
        assistant_message={"role": "assistant", "content": text or "（调用工具）"},
    )


def _contract(name: str, quanto: str, mark: str) -> Contract:
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


async def _zero_daily() -> DailyStats:
    return DailyStats(realized_pnl=Decimal(0), orders_today=0)


async def _make_paper_loop(tmp_path, provider: SeqProvider) -> SimpleNamespace:
    """paper 全链路决策循环（PaperGateway + drain_fills），审计快照隔离到 tmp_path。"""
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
    provider = SeqProvider(
        [
            _resp(
                "开多",
                [
                    ToolCall(
                        "place_order",
                        {"contract": "BTC_USDT", "size": 1, "stop_loss_price": 58000},
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
                        {"contract": "BTC_USDT", "size": 1, "leverage": 5, "stop_loss_price": 1},
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


# ---------- 真实网关 inline 落库 source 两态：llm_open / llm_close ----------


async def _make_inline_env(tmp_path) -> SimpleNamespace:
    """MockGateway + save_fills_inline=True（真实网关路径）：工具层直接落 trades。"""
    db = Database()
    await db.open(tmp_path / "tools.db")
    repo = Repo(db)
    gateway = MockGateway(contracts={"BTC_USDT": _contract("BTC_USDT", "0.001", "60000")})
    deps = ToolDeps(
        gateway=gateway,
        risk_engine=RiskEngine(),
        risk_config=RiskConfig(),
        watchlist=["BTC_USDT"],
        repo=repo,
        candles=CandleCache(gateway, ManualPriceSource()),
        triggers=TriggerManager(lambda t, p: None),
        daily_stats_fn=_zero_daily,
        mode="paper",
        round_id="r-test",
        save_fills_inline=True,
    )
    return SimpleNamespace(
        db=db, repo=repo, gateway=gateway, deps=deps, registry=ToolRegistry(deps)
    )


async def test_inline_trade_source_open_and_close(tmp_path):
    """无 drain 钩子（save_fills_inline=True）：已成交单由工具层直接落 trades 并标注。"""
    env = await _make_inline_env(tmp_path)
    try:
        out = await env.registry.execute(
            "place_order", {"contract": "BTC_USDT", "size": 1, "stop_loss_price": 58000}
        )
        assert out.risk_verdict == "allow", out.text
        out = await env.registry.execute("place_order", {"contract": "BTC_USDT", "close": True})
        assert out.risk_verdict == "allow", out.text

        trades = await env.repo.trades_between(0.0, time.time() + 1)
        assert [t.source for t in trades] == ["llm_open", "llm_close"]
    finally:
        await env.db.close()


async def test_inline_reduce_only_source_llm_close(tmp_path):
    """inline 路径 reduce_only 平仓单同样标注 llm_close（需求：close or reduce_only）。"""
    env = await _make_inline_env(tmp_path)
    try:
        out = await env.registry.execute(
            "place_order", {"contract": "BTC_USDT", "size": 1, "stop_loss_price": 58000}
        )
        assert out.risk_verdict == "allow", out.text
        out = await env.registry.execute(
            "place_order", {"contract": "BTC_USDT", "size": -1, "reduce_only": True}
        )
        assert out.risk_verdict == "allow", out.text

        trades = await env.repo.trades_between(0.0, time.time() + 1)
        assert [t.source for t in trades] == ["llm_open", "llm_close"]
    finally:
        await env.db.close()


async def test_inline_close_no_position_no_ghost_trade(tmp_path):
    """无持仓 close 单（LLM 路径）：无真实成交不得写入 trades 行。"""
    env = await _make_inline_env(tmp_path)
    try:
        out = await env.registry.execute("place_order", {"contract": "BTC_USDT", "close": True})
        assert out.risk_verdict == "allow", out.text  # 平仓豁免风控，网关侧 no-op
        assert await env.repo.trades_between(0.0, time.time() + 1) == []
    finally:
        await env.db.close()
