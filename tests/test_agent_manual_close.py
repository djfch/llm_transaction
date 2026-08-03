"""DecisionLoop.manual_close（用户手动平仓）测试：与 LLM 平仓同一风控路径，成交标注 user_close。

覆盖：
- paper 全链路（build_app）：手动平仓落 trades(source=user_close)、orders(is_close=1)，
  成交缓冲被直接消费，轮末 drain 不再重复落库（无双计）
- 真实网关路径（无 drain 钩子）：工具层不写 trades；orders 行带 trade_source=user_close，
  供 ExchangeFillSync 成交回报分类归属（成交落库见 test_agent_fill_sync.py）
- 风控拒绝（账户权益非正）→ 抛 ManualCloseRiskDenied（server 层映射 422），且未下单
- 平仓豁免语义与 LLM close 一致：白名单外合约持仓可平、kill_switch 下可平
- 合约不存在 → GatewayError 原样上抛
"""

from __future__ import annotations

import time
from collections import deque
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.agent import DecisionLoop, LLMResponse, ManualCloseRiskDenied, PromptLoader, ToolCall
from src.bootstrap import build_app
from src.config import AuditConfig, Settings, Watchlist
from src.gateway.base import Account, Contract, GatewayError, OrderNotFound, OrderRequest
from src.gateway.mock import MockGateway
from src.market.candles import CandleCache, ManualPriceSource
from src.market.triggers import TriggerManager
from src.memory import Database, Repo
from src.paper.engine import PaperGateway
from src.risk.engine import RiskEngine

BTC = "BTC_USDT"


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


async def _make_loop(tmp_path, *, gateway: MockGateway, watchlist: list[str]) -> SimpleNamespace:
    """MockGateway 决策循环（无 drain 钩子 → 工具层不写 trades，真实网关路径）。"""
    db = Database()
    await db.open(tmp_path / "agent.db")
    repo = Repo(db)
    prompt_path = tmp_path / "system_prompt.md"
    prompt_path.write_text("# 策略书\n稳健交易，控制回撤。", encoding="utf-8")
    settings = Settings(audit=AuditConfig(dir=str(tmp_path / "audit")))
    loop = DecisionLoop(
        settings=settings,
        watchlist=watchlist,
        provider=SeqProvider([]),
        gateway=gateway,
        risk_engine=RiskEngine(),
        repo=repo,
        candles=CandleCache(gateway, ManualPriceSource()),
        triggers=TriggerManager(lambda t, p: None),
        prompt_loader=PromptLoader(prompt_path),
    )
    return SimpleNamespace(db=db, repo=repo, gateway=gateway, loop=loop, settings=settings)


async def _close_order_flags(repo: Repo) -> list[int]:
    """orders 表全部行的 is_close 标记（list_orders 模型不含该列，直查 SQL）。"""
    cur = await repo._conn.execute("SELECT is_close FROM orders ORDER BY created_at, id")
    return [row[0] for row in await cur.fetchall()]


async def _trade_source_flags(repo: Repo) -> list[str]:
    """orders 表全部行的 trade_source 标记（直查 SQL，模型不含该列）。"""
    cur = await repo._conn.execute("SELECT trade_source FROM orders ORDER BY created_at, id")
    return [row[0] for row in await cur.fetchall()]


# ---------- paper 全链路：build_app 后手动平仓 ----------


async def test_manual_close_paper_full_chain(tmp_path):
    ctx = await build_app(
        Settings(audit=AuditConfig(dir=str(tmp_path / "audit"))),
        Watchlist(contracts=[BTC]),
        mock_llm=True,
        mock_market=True,
        db_path=tmp_path / "t.db",
    )
    try:
        gateway = ctx.gateway
        assert isinstance(gateway, PaperGateway)
        gateway.on_price(BTC, Decimal("60000"))
        # 直接经网关开出持仓（模拟既有仓位；不经工具层，故无 orders 行、fill 入缓冲）
        gateway.place_order(OrderRequest(contract=BTC, size=Decimal(1)))
        assert len(gateway.list_positions()) == 1

        result = await ctx.loop.manual_close(BTC)

        assert result["contract"] == BTC
        assert result["status"] == "finished"
        assert result["fill_price"] > 0
        assert result["text"]
        assert gateway.list_positions() == []  # 持仓已平
        # 成交落库：缓冲中夹带的开仓 fill 按标准标注（llm_open）一并落库，
        # 本单平仓 fill 标注 user_close；各一行，无双计
        trades = await ctx.repo.trades_between(0.0, time.time() + 1)
        assert [t.source for t in trades] == ["llm_open", "user_close"]
        assert await _close_order_flags(ctx.repo) == [1]  # 平仓单 is_close=1
        # 之后再跑一轮：缓冲已被 manual_close 消费，drain 无货可落 → 无重复成交行
        assert (await ctx.loop.run_once("timer")).ok
        trades_after = await ctx.repo.trades_between(0.0, time.time() + 1)
        assert len(trades_after) == 2
        assert await ctx.repo.daily_stats("paper", 0.0) is not None  # 统计可算（不重复计单）
    finally:
        await ctx.db.close()


# ---------- 真实网关路径（无 drain 钩子）：orders.trade_source=user_close ----------


async def test_manual_close_marks_order_trade_source_user_close(tmp_path):
    """真实网关路径：工具层不写 trades（成交由 fill_sync 按交易所回报落库）；
    本单 orders 行标 trade_source=user_close，供成交回报分类归属。"""
    gateway = MockGateway(contracts={BTC: _contract(BTC, "0.001", "60000")})
    gateway.place_order(OrderRequest(contract=BTC, size=Decimal(1)))  # 制造持仓
    env = await _make_loop(tmp_path, gateway=gateway, watchlist=[BTC])
    try:
        result = await env.loop.manual_close(BTC)

        assert result["status"] == "finished"
        assert result["fill_price"] == Decimal("60000")
        assert await env.repo.trades_between(0.0, time.time() + 1) == []  # 工具层不落 trades
        assert await _close_order_flags(env.repo) == [1]
        assert await _trade_source_flags(env.repo) == ["user_close"]
    finally:
        await env.db.close()


# ---------- 风控拒绝：抛 ManualCloseRiskDenied（server 映射 422），未下单 ----------


async def test_manual_close_risk_denied_raises(tmp_path):
    gateway = MockGateway(
        contracts={BTC: _contract(BTC, "0.001", "60000")},
        account=Account(available=Decimal("-1"), unrealised_pnl=Decimal(0)),  # 权益非正
    )
    env = await _make_loop(tmp_path, gateway=gateway, watchlist=[BTC])
    try:
        with pytest.raises(ManualCloseRiskDenied, match="账户权益非正"):
            await env.loop.manual_close(BTC)
        assert env.gateway.placed == []  # 风控拒绝，未下单
        assert await env.repo.trades_between(0.0, time.time() + 1) == []
    finally:
        await env.db.close()


async def test_manual_cancel_syncs_open_order_to_local_record(tmp_path):
    gateway = MockGateway(contracts={BTC: _contract(BTC, "0.001", "60000")})
    order = gateway.place_order(
        OrderRequest(contract=BTC, size=Decimal(2), price=Decimal("59000"), tif="gtc")
    )
    env = await _make_loop(tmp_path, gateway=gateway, watchlist=[BTC])
    try:
        await env.repo.save_order(
            order.id, "round-1", "paper", BTC, order.size, order.price, order.tif
        )

        result = await env.loop.manual_cancel_order(BTC, order.id)

        assert result == {
            "id": order.id,
            "contract": BTC,
            "status": "finished",
            "finish_as": "cancelled",
            "warning": "",
        }
        assert gateway.list_orders(status="open") == []
        [record] = await env.repo.list_orders("round-1")
        assert (record.status, record.finish_as) == ("finished", "cancelled")
    finally:
        await env.db.close()


async def test_manual_cancel_finds_target_on_second_open_orders_page(tmp_path, monkeypatch):
    gateway = MockGateway(contracts={BTC: _contract(BTC, "0.001", "60000")})
    orders = [
        gateway.place_order(
            OrderRequest(contract=BTC, size=Decimal(1), price=Decimal(59000), tif="gtc")
        )
        for _ in range(101)
    ]
    target = orders[-1]
    calls: list[dict[str, object]] = []
    original_list_orders = gateway.list_orders

    def record_list_orders(
        contract: str | None = None,
        status: str = "open",
        limit: int | None = None,
        offset: int = 0,
    ):
        calls.append({"contract": contract, "status": status, "limit": limit, "offset": offset})
        return original_list_orders(contract, status, limit, offset)

    monkeypatch.setattr(gateway, "list_orders", record_list_orders)
    env = await _make_loop(tmp_path, gateway=gateway, watchlist=[BTC])
    try:
        result = await env.loop.manual_cancel_order(BTC, target.id)

        assert result["id"] == target.id
        assert result["status"] == "finished"

        assert [call["offset"] for call in calls] == [0, 100]
        assert [call["limit"] for call in calls] == [100, 100]
    finally:
        await env.db.close()


async def test_manual_cancel_returns_warning_when_local_sync_fails(tmp_path, monkeypatch):
    gateway = MockGateway(contracts={BTC: _contract(BTC, "0.001", "60000")})
    order = gateway.place_order(
        OrderRequest(contract=BTC, size=Decimal(-2), price=Decimal("61000"), tif="gtc")
    )
    env = await _make_loop(tmp_path, gateway=gateway, watchlist=[BTC])

    async def fail_sync(*_args, **_kwargs):
        raise RuntimeError("local database offline")

    monkeypatch.setattr(env.repo, "update_order_status", fail_sync)
    try:
        result = await env.loop.manual_cancel_order(BTC, order.id)

        assert gateway.list_orders(status="open") == []
        assert "请勿重试撤单" in result["warning"]
        assert "local database offline" in result["warning"]
    finally:
        await env.db.close()


# ---------- 平仓豁免语义与 LLM close 一致：白名单外可平、kill_switch 下可平 ----------


async def test_manual_cancel_rejects_order_from_another_contract(tmp_path):
    eth = "ETH_USDT"
    gateway = MockGateway(
        contracts={
            BTC: _contract(BTC, "0.001", "60000"),
            eth: _contract(eth, "0.01", "3000"),
        }
    )
    order = gateway.place_order(
        OrderRequest(contract=BTC, size=Decimal(2), price=Decimal("59000"), tif="gtc")
    )
    env = await _make_loop(tmp_path, gateway=gateway, watchlist=[BTC, eth])
    try:
        with pytest.raises(OrderNotFound):
            await env.loop.manual_cancel_order(eth, order.id)

        assert [open_order.id for open_order in gateway.list_orders(BTC, "open")] == [order.id]
    finally:
        await env.db.close()


async def test_manual_close_non_watchlist_contract_allowed(tmp_path):
    gateway = MockGateway(contracts={"DOGE_USDT": _contract("DOGE_USDT", "0.001", "60000")})
    gateway.place_order(OrderRequest(contract="DOGE_USDT", size=Decimal(1)))
    env = await _make_loop(tmp_path, gateway=gateway, watchlist=[BTC])  # DOGE 不在白名单
    try:
        result = await env.loop.manual_close("DOGE_USDT")  # 平仓豁免白名单
        assert result["status"] == "finished"
        assert env.gateway.list_positions() == []
    finally:
        await env.db.close()


async def test_manual_close_exempt_kill_switch(tmp_path):
    gateway = MockGateway(contracts={BTC: _contract(BTC, "0.001", "60000")})
    gateway.place_order(OrderRequest(contract=BTC, size=Decimal(1)))
    env = await _make_loop(tmp_path, gateway=gateway, watchlist=[BTC])
    env.settings.risk.kill_switch = True  # 风控锁开启：平仓仍放行
    try:
        result = await env.loop.manual_close(BTC)
        assert result["status"] == "finished"
        assert env.gateway.list_positions() == []
    finally:
        await env.db.close()


# ---------- 合约不存在：GatewayError 原样上抛 ----------


async def test_manual_close_unknown_contract_raises_gateway_error(tmp_path):
    gateway = MockGateway(contracts={BTC: _contract(BTC, "0.001", "60000")})
    env = await _make_loop(tmp_path, gateway=gateway, watchlist=[BTC])
    try:
        with pytest.raises(GatewayError):
            await env.loop.manual_close("DOGE_USDT")
    finally:
        await env.db.close()


# ---------- 无持仓平仓：不落幽灵成交行、不谎称成交均价（重复调用幂等） ----------


async def test_manual_close_no_position_no_ghost_row(tmp_path):
    gateway = MockGateway(contracts={BTC: _contract(BTC, "0.001", "60000")})  # 无持仓
    env = await _make_loop(tmp_path, gateway=gateway, watchlist=[BTC])
    try:
        result = await env.loop.manual_close(BTC)
        assert "无持仓" in result["text"]  # 不谎称成交均价
        assert await env.repo.trades_between(0.0, time.time() + 1) == []  # 无幽灵成交行
        again = await env.loop.manual_close(BTC)  # 用户重复点击：幂等，仍无成交行
        assert "无持仓" in again["text"]
        assert await env.repo.trades_between(0.0, time.time() + 1) == []
    finally:
        await env.db.close()
