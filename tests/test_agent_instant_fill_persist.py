"""paper 新成交即时落库回归：行情回调驱动 drain，强平/止盈止损/挂单成交不等轮末上表。

覆盖：
- on_ticker 撮合产生新成交后即时落库并发 trades_updated（不跑决策轮）
- 归属继承：挂单成交继承原订单 round_id；tpsl-*/liquidation 无订单行 → round_id=""
- 归属查询异常降级：round_id="" 落库保记录，不丢成交、事件照发
- 无双计：即时 drain 后轮末兜底 drain 无新记录、无新事件；无成交的 tick 不发事件
- 防抢：manual_close 持锁覆盖「下单→drain→落库」全程，即时 drain 抢不走其成交
- 接线守护：build_app 装配后 on_ticker 与 DecisionLoop 共享同一 FillPersister
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from types import SimpleNamespace

from src.agent.fill_persist import FillPersister, _pending_drains
from src.agent.loop import DecisionLoop
from src.agent.prompts import PromptLoader
from src.agent.ticker_fanout import make_on_ticker
from src.bootstrap import build_app
from src.config import AuditConfig, PaperConfig, Settings, Watchlist
from src.gateway.base import Contract, OrderRequest, Ticker
from src.market.candles import CandleCache, ManualPriceSource
from src.market.triggers import TriggerManager
from src.memory import Database, Repo
from src.memory.models import Trade
from src.paper.account import FillRecord
from src.paper.engine import PaperGateway
from src.risk.engine import RiskEngine


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


def _ticker(contract: str, price: str) -> Ticker:
    return Ticker(
        contract=contract,
        last=Decimal(price),
        mark_price=Decimal(price),
        funding_rate=Decimal("0.0001"),
        high_24h=Decimal(price),
        low_24h=Decimal(price),
        change_percentage=Decimal(0),
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


def _make_gateway() -> PaperGateway:
    gateway = PaperGateway(PaperConfig(initial_equity=Decimal("10000")))
    gateway.upsert_contract(_contract("BTC_USDT", "0.001", "60000"))
    gateway.on_price("BTC_USDT", Decimal("60000"))
    return gateway


async def _wait_trades(repo: Repo, n: int, timeout: float = 2.0) -> list[Trade]:
    """轮询成交表直至 ≥n 条或超时（替代固定 sleep 等 fire-and-forget 落库任务，防 CI flake）。"""
    deadline = time.monotonic() + timeout
    while True:
        trades = await repo.trades_between(0.0, time.time() + 1)
        if len(trades) >= n:
            return trades
        if time.monotonic() > deadline:
            raise AssertionError(f"等待成交落库超时：期望 ≥{n} 条，实际 {len(trades)} 条")
        await asyncio.sleep(0.01)


async def _wait_pending_drains(timeout: float = 2.0) -> None:
    """等全部即时 drain 任务收尾（强引用集清空），替代固定 sleep。"""
    deadline = time.monotonic() + timeout
    while _pending_drains:
        if time.monotonic() > deadline:
            raise AssertionError(f"即时 drain 任务未收尾：{len(_pending_drains)} 个挂起")
        await asyncio.sleep(0.01)


async def test_instant_drain_persists_resting_order_fill(tmp_path):
    """挂单被行情触发：不跑决策轮即落库，继承原订单 round_id，事件契约与轮末一致。"""
    env = await _make_repo(tmp_path)
    gateway = _make_gateway()
    events: list[dict] = []
    persister = FillPersister(env.repo, "paper", events.append)
    on_ticker = make_on_ticker(gateway, TriggerManager(lambda t, p: None), fill_persister=persister)
    try:
        result = gateway.place_order(
            OrderRequest(contract="BTC_USDT", size=Decimal(1), price=Decimal("59000"))
        )
        await env.repo.save_order(
            order_id=result.id,
            round_id="r-old",
            mode="paper",
            contract="BTC_USDT",
            side_size=Decimal(1),
            price=Decimal("59000"),
        )
        on_ticker(_ticker("BTC_USDT", "59000"))  # 触发挂单成交 + 调度即时落库
        trades = await _wait_trades(env.repo, 1)
        assert trades[0].round_id == "r-old"  # 继承原下单轮
        assert trades[0].source == "llm_open"
        assert events == [
            {"type": "trades_updated", "data": {"contracts": ["BTC_USDT"], "count": 1}}
        ]
        # 无双计：轮末兜底 drain 无货可落
        assert await persister.drain_persist(gateway.drain_fills) == 0
        assert len(await env.repo.trades_between(0.0, time.time() + 1)) == 1
        # 无成交的 tick：缓冲为空，不调度任务、不发事件（同步无操作，直接断言）
        on_ticker(_ticker("BTC_USDT", "59100"))
        assert len(events) == 1
    finally:
        await env.db.close()


async def test_instant_drain_tpsl_liquidation_empty_attribution(tmp_path):
    """tpsl-*/liquidation 无订单行：round_id=""（前端可见不可点），source 标注准确。"""
    env = await _make_repo(tmp_path)
    gateway = _make_gateway()
    events: list[dict] = []
    persister = FillPersister(env.repo, "paper", events.append)
    on_ticker = make_on_ticker(gateway, TriggerManager(lambda t, p: None), fill_persister=persister)
    try:
        gateway.account.fills.extend(
            [
                _fill("tpsl-BTC_USDT-1", "BTC_USDT", -1, True),
                _fill("liquidation", "BTC_USDT", -1, True),
            ]
        )
        on_ticker(_ticker("BTC_USDT", "60000"))
        trades = await _wait_trades(env.repo, 2)
        assert {t.source for t in trades} == {"tpsl_close", "liquidation"}
        assert all(t.round_id == "" for t in trades)
        assert events == [
            {"type": "trades_updated", "data": {"contracts": ["BTC_USDT"], "count": 2}}
        ]
    finally:
        await env.db.close()


async def test_manual_close_holds_lock_against_instant_drain(tmp_path, monkeypatch):
    """manual_close 持锁全程：save_order 让出窗口时调度的即时 drain 抢不走本单成交。

    本单仍标 user_close 且只落一条；被阻塞的即时 drain 事后 drain 到空缓冲，无新记录。
    """
    env = await _make_repo(tmp_path)
    gateway = _make_gateway()
    events: list[dict] = []
    persister = FillPersister(env.repo, "paper", events.append)
    on_ticker = make_on_ticker(gateway, TriggerManager(lambda t, p: None), fill_persister=persister)
    prompt_path = tmp_path / "system_prompt.md"
    prompt_path.write_text("# 策略书\n稳健交易。", encoding="utf-8")
    loop = DecisionLoop(
        settings=Settings(audit=AuditConfig(dir=str(tmp_path / "audit"))),
        watchlist=["BTC_USDT"],
        provider=None,
        gateway=gateway,
        risk_engine=RiskEngine(),
        repo=env.repo,
        candles=CandleCache(gateway, ManualPriceSource()),
        triggers=TriggerManager(lambda t, p: None),
        prompt_loader=PromptLoader(prompt_path),
        drain_fills=gateway.drain_fills,
        fill_persister=persister,
    )
    try:
        gateway.place_order(OrderRequest(contract="BTC_USDT", size=Decimal(1)))  # 市价开多
        await persister.drain_persist(gateway.drain_fills)  # 开仓成交先落库，清空缓冲
        events.clear()

        reached = asyncio.Event()
        original_save_order = env.repo.save_order

        async def _slow_save_order(*args, **kwargs):
            reached.set()  # 平仓单已下单（成交已入缓冲），落订单行时让出事件循环
            await asyncio.sleep(0.05)
            return await original_save_order(*args, **kwargs)

        monkeypatch.setattr(env.repo, "save_order", _slow_save_order)
        task = asyncio.create_task(loop.manual_close("BTC_USDT"))
        await asyncio.wait_for(reached.wait(), timeout=1)
        on_ticker(_ticker("BTC_USDT", "60000"))  # 窗口内调度即时 drain（应被锁挡住）
        result = await task
        await _wait_pending_drains()  # 让被挡的 drain 任务收尾
        assert "成交均价" in result["text"]
        trades = await env.repo.trades_between(0.0, time.time() + 1)
        assert len(trades) == 2  # 开仓 1 + 平仓 1，无双计
        close_trade = [t for t in trades if t.source == "user_close"]
        assert len(close_trade) == 1
        assert events == [
            {"type": "trades_updated", "data": {"contracts": ["BTC_USDT"], "count": 1}}
        ]
    finally:
        await env.db.close()


async def test_attribution_query_failure_degrades_not_loses(tmp_path, monkeypatch):
    """归属查询异常：成交不丢——降级 round_id="" 落库保记录，事件照发。

    回归：order_round_id 曾在 try 之外，查询异常中止整批且缓冲已空 → 成交永久丢失。
    """
    env = await _make_repo(tmp_path)
    events: list[dict] = []
    persister = FillPersister(env.repo, "paper", events.append)

    async def _boom(order_id: str) -> str | None:
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(env.repo, "order_round_id", _boom)
    try:
        fills = [_fill("o1", "BTC_USDT", 1), _fill("o2", "BTC_USDT", -1, True)]
        failures = await persister.drain_persist(lambda: fills)
        assert failures == 0  # 落库本身未失败
        trades = await env.repo.trades_between(0.0, time.time() + 1)
        assert len(trades) == 2  # 两笔都保住了
        assert all(t.round_id == "" for t in trades)  # 降级为无归属
        assert events == [
            {"type": "trades_updated", "data": {"contracts": ["BTC_USDT"], "count": 2}}
        ]
    finally:
        await env.db.close()


async def test_build_app_shares_one_persister_between_ticker_and_loop(tmp_path):
    """build_app 接线守护：on_ticker 与 DecisionLoop 必须共享同一 FillPersister（同一把锁）。

    防抢 user_close / 无双计成立的前提是同一实例；DecisionLoop 缺省静默自建新实例，
    接错线（两把锁）不会有任何测试变红——此处显式断言实例同一性并做行为验证。
    """
    ctx = await build_app(
        Settings(),
        Watchlist(contracts=["BTC_USDT"]),
        mock_llm=True,
        mock_market=True,
        db_path=tmp_path / "t.db",
    )
    try:
        handler = ctx.source._on_ticker  # ManualPriceSource 保存的 on_ticker 闭包
        freevars = handler.__code__.co_freevars
        persister = handler.__closure__[freevars.index("fill_persister")].cell_contents
        assert persister is ctx.loop._persister  # 同一实例 = 同一把锁
        # 行为验证：产生成交后不跑决策轮，tick 即触发落库
        handler(_ticker("BTC_USDT", "60000"))  # 先注入行情快照（无成交，不调度）
        ctx.gateway.place_order(OrderRequest(contract="BTC_USDT", size=Decimal(1)))
        handler(_ticker("BTC_USDT", "60000"))  # 缓冲有货 → 调度即时落库
        trades = await _wait_trades(ctx.repo, 1)
        assert trades[0].contract == "BTC_USDT"
    finally:
        await ctx.db.close()
