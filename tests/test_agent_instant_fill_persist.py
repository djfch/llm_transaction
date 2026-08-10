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
    """构造测试用合约元数据（固定费率/档位，仅名称、乘数、标记价可变）。

    参数：
        name: str，合约名（如 "BTC_USDT"）
        quanto: str，合约乘数（每张对应的基础货币数量）
        mark: str，标记价格（影响撮合与强平判断）

    返回：
        Contract：可直接喂给 PaperGateway.upsert_contract 的合约对象
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


def _ticker(contract: str, price: str) -> Ticker:
    """构造测试用行情快照（最新价/标记价/24h 高低均取同一价格）。

    参数：
        contract: str，合约名（如 "BTC_USDT"）
        price: str，统一填充到各价格字段的行情价

    返回：
        Ticker：可直接喂给 on_ticker 回调的行情对象
    """
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
    """构造测试用成交流水（固定价 60000、固定手续费、无盈亏、taker）。

    参数：
        order_id: str，订单标识（如 "tpsl-BTC_USDT-1"、"liquidation"），落库时据此推 source
        contract: str，合约名
        size: int，成交张数（负数表示减仓方向）
        is_close: bool，是否平仓成交，默认 False（开仓）

    返回：
        FillRecord：可直接塞进 paper 账户成交缓冲的成交记录
    """
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
    """在临时目录打开独立数据库并配套 Repo。

    参数：
        tmp_path: Path，pytest 临时目录夹具，agent.db 落在其中（用例间隔离）

    返回：
        SimpleNamespace：带 db（已打开的数据库，需调用方 finally 关闭）与
        repo（基于该库的仓储对象）两个属性
    """
    db = Database()
    await db.open(tmp_path / "agent.db")
    return SimpleNamespace(db=db, repo=Repo(db))


def _make_gateway() -> PaperGateway:
    """构造已注册 BTC_USDT 合约、初始价 60000 的模拟撮合网关。

    参数：无

    返回：
        PaperGateway：初始权益 10000、已完成一次价格注入的 paper 网关
    """
    gateway = PaperGateway(PaperConfig(initial_equity=Decimal("10000")))
    gateway.upsert_contract(_contract("BTC_USDT", "0.001", "60000"))
    gateway.on_price("BTC_USDT", Decimal("60000"))
    return gateway


async def _wait_trades(repo: Repo, n: int, timeout: float = 2.0) -> list[Trade]:
    """轮询成交表直至 ≥n 条或超时（替代固定 sleep 等 fire-and-forget 落库任务，防 CI flake）。

    参数：
        repo: Repo，连接测试数据库的仓储实例
        n: int，需要读取或生成的记录数量
        timeout: float，最长等待秒数

    返回：
        list[Trade]，达到期望数量后从成交表读取的记录列表

    异常：
        AssertionError，等待成交记录达到期望数量超时时抛出
    """
    deadline = time.monotonic() + timeout
    while True:
        trades = await repo.trades_between(0.0, time.time() + 1)
        if len(trades) >= n:
            return trades
        if time.monotonic() > deadline:
            raise AssertionError(f"等待成交落库超时：期望 ≥{n} 条，实际 {len(trades)} 条")
        await asyncio.sleep(0.01)


async def _wait_pending_drains(timeout: float = 2.0) -> None:
    """等全部即时 drain 任务收尾（强引用集清空），替代固定 sleep。

    参数：
        timeout: float，最长等待秒数

    返回：
        None，执行上述模拟操作或副作用，无返回值

    异常：
        AssertionError，即时成交落库任务在期限内未收尾时抛出
    """
    deadline = time.monotonic() + timeout
    while _pending_drains:
        if time.monotonic() > deadline:
            raise AssertionError(f"即时 drain 任务未收尾：{len(_pending_drains)} 个挂起")
        await asyncio.sleep(0.01)


async def test_instant_drain_persists_resting_order_fill(tmp_path):
    """挂单被行情触发：不跑决策轮即落库，继承原订单 round_id，事件契约与轮末一致。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证上述行为，无返回值
    """
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
    """tpsl-*/liquidation 无订单行：round_id=""（前端可见不可点），source 标注准确。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证上述行为，无返回值
    """
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

    参数：
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: pytest.MonkeyPatch，用于隔离并替换依赖或环境变量的 pytest 夹具

    返回：
        None，通过断言验证上述行为，无返回值
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
            """慢速版 save_order：落订单行前让出事件循环，制造即时 drain 的抢锁窗口。

            参数：
                args: tuple，透传给原始 save_order 的位置参数
                kwargs: dict，透传给原始 save_order 的关键字参数

            返回：
                原始 save_order 的返回值（订单行落库结果）；副作用为置位 reached
                事件并 sleep 0.05 秒让出调度
            """
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

    参数：
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: pytest.MonkeyPatch，用于隔离并替换依赖或环境变量的 pytest 夹具

    返回：
        None，通过断言验证上述行为，无返回值
    """
    env = await _make_repo(tmp_path)
    events: list[dict] = []
    persister = FillPersister(env.repo, "paper", events.append)

    async def _boom(order_id: str) -> str | None:
        """模拟 order_round_id 查询数据库时异常（monkeypatch 替换件）。

        参数：
            order_id: str，订单标识（本替身不使用）

        返回：
            str | None：永不返回，调用即抛异常

        异常：
            RuntimeError：总是抛出，模拟数据库抖动
        """
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

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证上述行为，无返回值
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
