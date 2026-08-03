"""trades_updated 事件发射回归：成交落库成功后统一推 WS 失效信号。

契约：{"type": "trades_updated", "data": {"contracts": [...], "count": N}}
（本批落库成功的合约去重 + 成功笔数；仅 ≥1 笔成功才发，全部失败/无成交不发）。
本文件覆盖 FillPersister（轮末 drain / 手动平仓 / 行情即时 drain）；
真实网关 ExchangeFillSync 路径见 test_agent_fill_sync.py。
"""

from __future__ import annotations

import time
from collections import deque
from decimal import Decimal
from types import SimpleNamespace

from src.agent import DecisionLoop, LLMResponse, PromptLoader, ToolCall
from src.agent.fill_persist import FillPersister
from src.config import AuditConfig, PaperConfig, Settings
from src.gateway.base import Contract
from src.market.candles import CandleCache, ManualPriceSource
from src.market.triggers import TriggerManager
from src.memory import Database, Repo
from src.paper.account import FillRecord
from src.paper.engine import PaperGateway
from src.risk.engine import RiskEngine


class SeqProvider:
    """预置响应序列的 mock provider。"""

    def __init__(self, responses: list) -> None:
        self._responses = deque(responses)

    async def chat(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        if not self._responses:
            return LLMResponse(text="（无更多预置响应）", raw="{}")
        return self._responses.popleft()

    def tool_result_message(self, call: ToolCall, result: str) -> dict:
        return {"role": "tool", "tool_call_id": call.call_id, "content": result}


def _resp(text: str, calls: list[ToolCall]) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=calls,
        raw="{}",
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


def _fill(order_id: str, contract: str, size: int, is_close: bool = False) -> FillRecord:
    return FillRecord(
        order_id=order_id,
        contract=contract,
        size=Decimal(size),
        price=Decimal("60000"),
        fee=Decimal("0.1"),
        realized_pnl=Decimal(0),
        maker=False,
        is_close=is_close,
    )


async def _make_repo(tmp_path) -> SimpleNamespace:
    db = Database()
    await db.open(tmp_path / "agent.db")
    return SimpleNamespace(db=db, repo=Repo(db))


# ---------- FillPersister 单元层：事件发射与失败语义 ----------


async def test_persister_emits_once_with_payload(tmp_path):
    """一批 3 笔（2 合约）全部落库成功 → 发一次事件，contracts 去重排序、count=3。"""
    env = await _make_repo(tmp_path)
    events: list[dict] = []
    try:
        fills = [
            _fill("o1", "BTC_USDT", 1),
            _fill("o2", "ETH_USDT", 2),
            _fill("o3", "BTC_USDT", -1, True),
        ]
        failures = await FillPersister(env.repo, "paper", events.append).drain_persist(
            lambda: fills
        )
        assert failures == 0
        assert events == [
            {
                "type": "trades_updated",
                "data": {"contracts": ["BTC_USDT", "ETH_USDT"], "count": 3},
            }
        ]
        assert len(await env.repo.trades_between(0.0, time.time() + 1)) == 3
    finally:
        await env.db.close()


async def test_persister_all_fail_no_event(tmp_path, monkeypatch):
    """全部落库失败 → 不发事件（失败仅记日志，不重试）。"""
    env = await _make_repo(tmp_path)
    events: list[dict] = []

    async def _fail_save(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(env.repo, "save_trade", _fail_save)
    try:
        failures = await FillPersister(env.repo, "paper", events.append).drain_persist(
            lambda: [_fill("o1", "BTC_USDT", 1)]
        )
        assert failures == 1
        assert events == []
    finally:
        await env.db.close()


async def test_persister_partial_failure_payload(tmp_path, monkeypatch):
    """部分失败：count=成功笔数、contracts 仅含成功合约（失败笔不进事件）。"""
    env = await _make_repo(tmp_path)
    events: list[dict] = []
    original = env.repo.save_trade

    async def _fail_eth(**kwargs):
        if kwargs["contract"] == "ETH_USDT":
            raise RuntimeError("db down")
        return await original(**kwargs)

    monkeypatch.setattr(env.repo, "save_trade", _fail_eth)
    try:
        fills = [
            _fill("o1", "BTC_USDT", 1),
            _fill("o2", "ETH_USDT", 2),
            _fill("o3", "BTC_USDT", -1, True),
        ]
        failures = await FillPersister(env.repo, "paper", events.append).drain_persist(
            lambda: fills
        )
        assert failures == 1
        assert events == [
            {"type": "trades_updated", "data": {"contracts": ["BTC_USDT"], "count": 2}}
        ]
        assert len(await env.repo.trades_between(0.0, time.time() + 1)) == 2
    finally:
        await env.db.close()


async def test_persister_without_notify_event_compatible(tmp_path):
    """不传 notify_event → 正常落库不报错。"""
    env = await _make_repo(tmp_path)
    try:
        failures = await FillPersister(env.repo, "paper").drain_persist(
            lambda: [_fill("o1", "BTC_USDT", 1)]
        )
        assert failures == 0
        assert len(await env.repo.trades_between(0.0, time.time() + 1)) == 1
    finally:
        await env.db.close()


async def test_persister_empty_batch_no_event(tmp_path):
    """空批次（drain 无成交）→ 不发事件。"""
    env = await _make_repo(tmp_path)
    events: list[dict] = []
    try:
        failures = await FillPersister(env.repo, "paper", events.append).drain_persist(list)
        assert failures == 0
        assert events == []
    finally:
        await env.db.close()


# ---------- paper 决策轮 drain 路径 ----------


async def _make_paper_loop(tmp_path, provider: SeqProvider, events: list[dict]) -> SimpleNamespace:
    """paper 全链路决策循环（PaperGateway + drain_fills + notify_event 捕获）。"""
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
        notify_event=events.append,
    )
    return SimpleNamespace(db=db, repo=repo, gateway=gateway, loop=loop)


async def test_drain_round_emits_event_only_when_fills(tmp_path):
    """有成交的轮次轮末发事件；无成交的轮次不发。"""
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
            _resp("好", []),  # round1：drain 落 1 笔 llm_open → 发事件
            _resp("观望", []),  # round2：无成交 → 不发
        ]
    )
    events: list[dict] = []
    env = await _make_paper_loop(tmp_path, provider, events)
    try:
        assert (await env.loop.run_once("timer")).ok
        assert events == [
            {"type": "trades_updated", "data": {"contracts": ["BTC_USDT"], "count": 1}}
        ]
        assert (await env.loop.run_once("timer")).ok
        assert len(events) == 1  # 第二轮无成交，不新增事件
    finally:
        await env.db.close()


async def test_manual_close_emits_event(tmp_path):
    """手动平仓：本单成交落库后发事件（夹带批为空不发，本单批发一次）。"""
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
            _resp("好", []),  # round1：开仓成交 drain（发 1 次，随后清空计数）
        ]
    )
    events: list[dict] = []
    env = await _make_paper_loop(tmp_path, provider, events)
    try:
        assert (await env.loop.run_once("timer")).ok
        events.clear()
        result = await env.loop.manual_close("BTC_USDT")
        assert "成交均价" in result["text"]
        assert events == [
            {"type": "trades_updated", "data": {"contracts": ["BTC_USDT"], "count": 1}}
        ]
    finally:
        await env.db.close()
