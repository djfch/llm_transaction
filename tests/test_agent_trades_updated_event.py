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
        """保存预置响应序列，供 chat 按调用顺序依次弹出。

        参数：
            responses: list，预置的 LLM 响应列表，按顺序消费

        返回：
            None，就地初始化内部响应队列
        """
        self._responses = deque(responses)

    async def chat(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        """按序弹出预置响应；序列弹尽时返回无工具调用的占位响应。

        参数：
            system: str，系统提示词（mock 忽略）
            messages: list[dict]，对话消息列表（mock 忽略）
            tools: list[dict]，可用工具定义（mock 忽略）

        返回：
            LLMResponse：预置序列中的下一条响应；序列为空时返回固定文本的占位响应
        """
        if not self._responses:
            return LLMResponse(text="（无更多预置响应）", raw="{}")
        return self._responses.popleft()

    def tool_result_message(self, call: ToolCall, result: str) -> dict:
        """把工具执行结果包装成 provider 约定的 tool 角色消息。

        参数：
            call: ToolCall，已执行的工具调用，取其 call_id 关联结果
            result: str，工具执行结果文本

        返回：
            dict：{"role": "tool", "tool_call_id": ..., "content": ...} 形式的消息
        """
        return {"role": "tool", "tool_call_id": call.call_id, "content": result}


def _resp(text: str, calls: list[ToolCall]) -> LLMResponse:
    """构造带指定文本和工具调用的模拟模型响应。

    参数：
        text: str，助手回复正文
        calls: list[ToolCall]，本轮提出的工具调用列表

    返回：
        LLMResponse，可直接放入 SeqProvider 响应序列的模型响应
    """
    return LLMResponse(
        text=text,
        tool_calls=calls,
        raw="{}",
        assistant_message={"role": "assistant", "content": text or "（调用工具）"},
    )


def _contract(name: str, quanto: str, mark: str) -> Contract:
    """构造指定名称、合约乘数与标记价的测试合约元数据。

    参数：
        name: str，永续合约名称
        quanto: str，合约乘数的十进制字符串
        mark: str，标记价的十进制字符串

    返回：
        Contract，带固定手续费和下单边界的测试合约
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


def _fill(order_id: str, contract: str, size: int, is_close: bool = False) -> FillRecord:
    """构造用于成交持久化测试的固定价格成交记录。

    参数：
        order_id: str，成交关联的订单编号
        contract: str，成交所属合约
        size: int，带方向的成交张数
        is_close: bool，是否属于平仓成交，默认否

    返回：
        FillRecord，价格 60000、手续费 0.1 的模拟成交
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
    """打开临时数据库并返回数据库与仓储测试环境。

    参数：
        tmp_path: Path，pytest 临时目录

    返回：
        SimpleNamespace，包含 db(数据库)与 repo(仓储)
    """
    db = Database()
    await db.open(tmp_path / "agent.db")
    return SimpleNamespace(db=db, repo=Repo(db))


# ---------- FillPersister 单元层：事件发射与失败语义 ----------


async def test_persister_emits_once_with_payload(tmp_path):
    """验证三笔两合约成交全部落库后只发一次去重排序的更新事件。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证失败数、事件载荷和落库笔数
    """
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
    """验证整批成交全部落库失败时记录失败但不发更新事件。

    参数：
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: MonkeyPatch，用于把成交保存替换为失败桩

    返回：
        None，通过断言验证失败计数和空事件列表
    """
    env = await _make_repo(tmp_path)
    events: list[dict] = []

    async def _fail_save(**kwargs):
        """模拟任何成交保存请求都发生数据库故障。

        参数：
            kwargs: dict，成交保存关键字参数，本桩不读取其内容

        返回：
            None，本函数始终在返回前抛出异常

        异常：
            RuntimeError: 每次调用都抛出，用于模拟数据库不可用
        """
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
    """验证部分成交落库失败时事件只统计成功笔数与成功合约。

    参数：
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: MonkeyPatch，用于注入按合约失败的保存桩

    返回：
        None，通过断言验证失败数、事件载荷和实际落库笔数
    """
    env = await _make_repo(tmp_path)
    events: list[dict] = []
    original = env.repo.save_trade

    async def _fail_eth(**kwargs):
        """仅让 ETH_USDT 成交保存失败，其余请求委托原保存方法。

        参数：
            kwargs: dict，包含 contract(合约)等成交保存字段

        返回：
            TradeRecord，非 ETH_USDT 成交的持久化记录

        异常：
            RuntimeError: contract(合约)为 ETH_USDT 时模拟数据库故障
        """
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
    """验证未接入事件通知回调时成交持久化仍保持兼容并正常落库。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证零失败与一笔落库记录
    """
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
    """验证成交排空返回空批次时不报失败也不发更新事件。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证失败数为零且事件列表为空
    """
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
    """组装包含模拟撮合、成交排空与事件捕获的 paper 决策循环。

    参数：
        tmp_path: Path，pytest 临时目录，用于隔离数据库、审计与提示词文件
        provider: SeqProvider，按序提供决策响应的模拟模型
        events: list[dict]，收集更新事件的外部列表

    返回：
        SimpleNamespace，包含 db(数据库)、repo(仓储)、gateway(网关)和 loop(决策循环)
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
        notify_event=events.append,
    )
    return SimpleNamespace(db=db, repo=repo, gateway=gateway, loop=loop)


async def test_drain_round_emits_event_only_when_fills(tmp_path):
    """验证决策轮仅在轮末排空到成交时发送更新事件。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证有成交轮次发一次、无成交轮次不新增事件
    """
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
        trade_events = [event for event in events if event["type"] == "trades_updated"]
        assert trade_events == [
            {"type": "trades_updated", "data": {"contracts": ["BTC_USDT"], "count": 1}}
        ]
        assert (await env.loop.run_once("timer")).ok
        assert len([event for event in events if event["type"] == "trades_updated"]) == 1
    finally:
        await env.db.close()


async def test_manual_close_emits_event(tmp_path):
    """验证手动平仓成交落库后发送一次对应合约的更新事件。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证平仓结果文本及事件载荷
    """
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


async def test_failed_fill_retried_with_idempotent_key(tmp_path):
    """落库失败笔进入重试队列且幂等键防双计（issue #67）。

    参数：
        tmp_path: Path，pytest 临时目录夹具

    返回：
        None，断言首次失败笔保留、下轮重试成功且 trades 表只有一笔
    """
    db = Database()
    await db.open(tmp_path / "agent.db")
    repo = Repo(db)
    calls = {"n": 0}
    original = repo.save_trade

    async def flaky_save_trade(*args, **kwargs):
        """首次调用抛异常模拟写库故障，其后放行（记录调用次数）。"""
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("disk io error")
        return await original(*args, **kwargs)

    repo.save_trade = flaky_save_trade  # 模拟首轮写库故障

    persister = FillPersister(repo, "paper")
    fill = FillRecord(
        order_id="t-1",
        contract="BTC_USDT",
        size=Decimal("1"),
        price=Decimal("100"),
        fee=Decimal("0"),
        realized_pnl=Decimal("0"),
        maker=False,
        is_close=False,
        trade_id="t-1:1",
    )
    try:
        failures = await persister.persist_locked([fill])
        assert failures == 1 and persister._pending == [fill]  # 失败笔留缓冲
        failures = await persister.persist_locked([])  # 下轮重试
        assert failures == 0 and persister._pending == []
        rows = await repo.trades_between(0, time.time() + 1, mode="paper")
        assert len(rows) == 1  # 幂等：只有一笔
        assert rows[0].contract == "BTC_USDT"
    finally:
        await db.close()
