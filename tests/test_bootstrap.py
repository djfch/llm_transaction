"""bootstrap 接线测试：DecisionLoop 依赖注入、server runtime 同步、资金费周期、预警生命周期。

覆盖以下接线不变量：
- paper 模式注入 drain_fills；真实网关为 None（trades 由 fill_sync 按交易所成交回报落库）
- persist_kill_switch 写回 config.yaml；audit 与 server 共用同一实例
- ServerDeps.runtime_settings / runtime_watchlist 与决策循环共享同一对象
- 资金费按合约 funding_interval 结算（8h 周期，1 小时内不重复结算）
- 预警线：内存唯一存储，触发即一次性移除并抢醒调度器；重启后不重建，由 LLM 决定是否重设
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from src.bootstrap import AppContext, _default_contract, build_app, run_app
from src.config import Settings, Watchlist
from src.config_io import write_settings
from src.gateway.base import OrderRequest, Ticker
from src.paper.engine import PaperGateway
from src.paper.funding_patrol import settle_due_funding
from src.research.setup import build_research as _build_research_impl
from src.review.strategy import StrategyValidationError

BTC = "BTC_USDT"
WATCHLIST = Watchlist(contracts=[BTC])


@pytest.fixture
async def build_ctx(tmp_path: Path) -> AsyncIterator[Callable[..., AppContext]]:
    """提供统一构建应用上下文的异步工厂并在用例结束后关闭全部数据库。

    参数：
        tmp_path: Path，pytest 临时目录，用于隔离数据库等运行文件

    返回：
        AsyncIterator[Callable[..., AppContext]]，生成可按需覆盖配置的异步上下文工厂
    """
    ctxs: list[AppContext] = []

    async def _factory(settings: Settings | None = None, **kwargs) -> AppContext:
        """按统一 mock 配置组装应用上下文，并登记到清理列表由 fixture 收尾。

        参数：
            settings: Settings | None，应用配置；传 None 时使用默认 Settings()
            **kwargs: 透传给 build_app 的额外参数（如 config_path）

        返回：
            AppContext：已组装的应用上下文，其数据库在 fixture 结束时统一关闭
        """
        ctx = await build_app(
            settings or Settings(),
            WATCHLIST,
            mock_llm=True,
            mock_market=True,
            db_path=tmp_path / "t.db",
            **kwargs,
        )
        ctxs.append(ctx)
        return ctx

    yield _factory
    for ctx in ctxs:
        await ctx.db.close()


def _ticker(price: Decimal) -> Ticker:
    """构造指定价格的 BTC 行情快照（资金费率 0.0001，24h 高低价同取该价）。

    参数：
        price: Decimal，最新价，同时作为标记价与 24h 高/低价

    返回：
        Ticker：BTC_USDT 的行情快照对象
    """
    return Ticker(
        contract=BTC,
        last=price,
        mark_price=price,
        funding_rate=Decimal("0.0001"),
        high_24h=price,
        low_24h=price,
        change_percentage=Decimal("0.5"),
    )


# ---------- DecisionLoop 接线 ----------


async def test_paper_mode_injects_drain_fills(build_ctx):
    """验证 paper 模式把模拟成交排空函数注入决策循环且不创建私有成交订阅。

    参数：
        build_ctx: Callable，应用上下文异步构建夹具

    返回：
        None，通过断言验证网关类型、排空接线和空成交订阅
    """
    ctx = await build_ctx()
    assert isinstance(ctx.gateway, PaperGateway)
    assert ctx.loop._drain_fills == ctx.gateway.drain_fills  # bound method 同 func+self 即相等
    assert ctx.trade_feed is None


async def test_real_gateway_mode_has_no_drain_fills(build_ctx):
    """验证 testnet 模式不注入 paper 成交排空函数且模拟行情下不建私有订阅。

    参数：
        build_ctx: Callable，应用上下文异步构建夹具

    返回：
        None，通过断言验证排空函数和成交订阅均为空
    """
    ctx = await build_ctx(Settings(mode="testnet"))
    assert ctx.loop._drain_fills is None
    assert ctx.trade_feed is None  # mock_market=True 时不接私有 WS（真实装配见 build_app）


async def test_persist_kill_switch_writes_config(tmp_path: Path, build_ctx):
    """验证决策循环的风控锁持久化回调把真值写回配置文件。

    参数：
        tmp_path: Path，pytest 临时目录
        build_ctx: Callable，应用上下文异步构建夹具

    返回：
        None，通过断言验证 risk.kill_switch(风控锁)已写为 true
    """
    config_path = tmp_path / "config.yaml"
    write_settings({}, config_path)  # 先落一份合法配置
    ctx = await build_ctx(config_path=config_path)
    ctx.loop._persist_kill_switch(True)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert raw["risk"]["kill_switch"] is True


async def test_audit_shared_between_loop_and_server(build_ctx):
    """验证决策循环与服务器共享同一审计轨迹实例以保持单点落库。

    参数：
        build_ctx: Callable，应用上下文异步构建夹具

    返回：
        None，通过对象身份断言验证审计实例共享
    """
    ctx = await build_ctx()
    assert ctx.server_deps is not None
    assert ctx.loop._audit is ctx.server_deps.audit_trail


# ---------- server runtime 同步 ----------


async def test_server_runtime_shares_settings_and_watchlist(build_ctx):
    """验证服务器、应用上下文与决策工具共享同一配置和白名单对象。

    参数：
        build_ctx: Callable，应用上下文异步构建夹具

    返回：
        None，通过对象身份断言验证运行时原地更新可跨层可见
    """
    ctx = await build_ctx()
    deps = ctx.server_deps
    assert deps is not None
    assert deps.runtime_settings is ctx.settings
    assert deps.runtime_watchlist is ctx.watchlist
    assert ctx.loop._deps.watchlist is ctx.watchlist  # 决策循环持有的也是同一 list


# ---------- 资金费按 funding_interval 结算 ----------


def _paper_with_long_position() -> PaperGateway:
    """构造持有一张 BTC 多单且资金费周期为八小时的 paper 网关。

    参数：无

    返回：
        PaperGateway，已录入合约、价格并成交一张多单的模拟网关
    """
    gateway = PaperGateway(Settings().paper)
    gateway.upsert_contract(_default_contract(BTC, Decimal("50000")))
    gateway.on_price(BTC, Decimal("50000"))
    gateway.place_order(OrderRequest(contract=BTC, size=Decimal(1)))
    return gateway


def test_funding_settles_only_when_interval_due():
    """验证资金费首次结算后八小时内不重复扣费且到期才再次结算。

    参数：无

    返回：
        None，通过断言验证一小时与八小时两个时间边界
    """
    gateway = _paper_with_long_position()
    last_settled: dict[str, float] = {}
    assert settle_due_funding(gateway, last_settled, now=1000.0) == [BTC]  # 首次见到持仓即结算
    first = gateway.account.total_funding
    assert first != 0
    assert settle_due_funding(gateway, last_settled, now=1000.0 + 3600) == []  # 1h < 8h 不结算
    assert gateway.account.total_funding == first
    assert settle_due_funding(gateway, last_settled, now=1000.0 + 28800) == [BTC]  # 到达周期再结算
    assert gateway.account.total_funding != first


# ---------- 预警线生命周期（内存唯一存储） ----------


async def test_trigger_fire_removes_from_memory_and_wakes(build_ctx):
    """验证价格预警触发后从内存移除并以精确原因抢醒调度器。

    参数：
        build_ctx: Callable，应用上下文异步构建夹具

    返回：
        None，通过断言验证预警单次语义和唤醒原因
    """
    ctx = await build_ctx()
    ctx.triggers.add(BTC, ">=", Decimal("60000"))
    wakes: list[str] = []
    orig_wake_now = ctx.scheduler.wake_now
    ctx.scheduler.wake_now = lambda reason: wakes.append(reason) or orig_wake_now(reason)

    await ctx.source.push_ticker(_ticker(Decimal("60000")))  # type: ignore[attr-defined]

    assert ctx.triggers.list() == []  # 一次性语义：触发即移除
    assert wakes == [f"price_trigger:{BTC}@60000"]


async def test_triggers_not_rebuilt_on_restart(build_ctx):
    """验证应用重新组装后不会从持久层重建仅存于内存的价格预警。

    参数：
        build_ctx: Callable，应用上下文异步构建夹具

    返回：
        None，通过断言验证旧上下文有预警而新上下文为空
    """
    ctx = await build_ctx()
    ctx.triggers.add(BTC, ">=", Decimal("60000"))
    assert len(ctx.triggers.list()) == 1
    ctx2 = await build_ctx()  # 同一 db_path 重新组装 = 模拟进程重启：内存索引全新
    assert ctx2.triggers.list() == []


# ---------- server 写操作接线 ----------


async def test_server_deps_wires_trading_callbacks(build_ctx):
    """验证 paper 模式服务器依赖完整接入手动平仓、启停和账户重置回调。

    参数：
        build_ctx: Callable，应用上下文异步构建夹具

    返回：
        None，通过断言验证四个交易控制回调均可用
    """
    ctx = await build_ctx()
    deps = ctx.server_deps
    assert deps is not None
    assert callable(deps.manual_close)
    assert deps.agent_start is not None and deps.agent_stop is not None
    assert deps.paper_reset is not None  # paper 模式接 PaperGateway.reset_account


async def test_server_deps_no_paper_reset_in_testnet(build_ctx):
    """验证 testnet 模式不会接入仅适用于 paper 的账户重置回调。

    参数：
        build_ctx: Callable，应用上下文异步构建夹具

    返回：
        None，通过断言验证 paper_reset(模拟账户重置)为空
    """
    ctx = await build_ctx(Settings(mode="testnet"))
    assert ctx.server_deps is not None
    assert ctx.server_deps.paper_reset is None


async def test_agent_start_stop_callbacks_drive_scheduler(build_ctx):
    """验证 agent 启停回调真实驱动调度器并同步更新运行状态。

    参数：
        build_ctx: Callable，应用上下文异步构建夹具

    返回：
        None，通过断言验证启动前、启动后与停止后的状态
    """
    ctx = await build_ctx()
    deps = ctx.server_deps
    assert deps is not None and deps.status_provider is not None
    assert deps.status_provider()["agent_running"] is False  # 调度器未启动
    await deps.agent_start()
    assert ctx.scheduler.is_running
    assert deps.status_provider()["agent_running"] is True
    await deps.agent_stop()
    assert not ctx.scheduler.is_running
    assert deps.status_provider()["agent_running"] is False


async def test_agent_start_fires_first_round_immediately(build_ctx):
    """验证手动启动 agent 会立即触发首轮而无需等待默认定时器。

    参数：
        build_ctx: Callable，应用上下文异步构建夹具

    返回：
        None，通过断言验证首个事件类型和 manual_start(手动启动)来源
    """
    ctx = await build_ctx()
    deps = ctx.server_deps
    assert deps is not None
    await deps.agent_start()
    try:
        first = await asyncio.wait_for(ctx.event_queue.get(), timeout=5)
    finally:
        await deps.agent_stop()
    assert first["type"] == "round_start"
    assert first["data"]["wake_source"] == "manual_start"


async def test_status_provider_includes_in_round(build_ctx):
    """验证状态提供器始终暴露当前是否处于决策轮的字段。

    参数：
        build_ctx: Callable，应用上下文异步构建夹具

    返回：
        None，通过断言验证 in_round(决策轮进行中)存在且初始为假
    """
    ctx = await build_ctx()
    deps = ctx.server_deps
    assert deps is not None and deps.status_provider is not None
    status = deps.status_provider()
    assert "in_round" in status
    assert status["in_round"] is False


async def test_on_wake_pushes_round_start_then_round(build_ctx):
    """验证开始事件发出时新审计行已存在，随后才发送结束事件。

    参数：
        build_ctx: Callable，应用上下文异步构建夹具

    返回：
        None，断言事件顺序、唤醒来源及 round_start 对应审计行已可查询
    """
    ctx = await build_ctx()  # fixture 默认 mock_llm + mock_market
    await ctx.scheduler.start()
    try:
        ctx.scheduler.wake_now("test_event_order")
        first = await asyncio.wait_for(ctx.event_queue.get(), timeout=5)
        started = await ctx.repo.get_audit_round(first["data"]["round_id"])
        second = await asyncio.wait_for(ctx.event_queue.get(), timeout=5)
    finally:
        await ctx.scheduler.stop()
    assert first["type"] == "round_start"
    assert first["data"]["wake_source"] == "test_event_order"
    assert started is not None
    assert started.ended_at is None
    assert second["type"] == "round"


# ---------- 研报子系统接线 ----------


async def test_research_subsystem_assembled(build_ctx):
    """验证研报 agent、调度器和服务器手动运行回调来自同一子系统装配。

    参数：
        build_ctx: Callable，应用上下文异步构建夹具

    返回：
        None，通过断言验证研报组件存在且运行回调同源
    """
    ctx = await build_ctx()
    assert ctx.research.agent is not None
    assert ctx.research.scheduler is not None
    deps = ctx.server_deps
    assert deps is not None
    assert deps.research_run == ctx.research.scheduler.run_now


async def test_build_research_receives_notify_event(build_ctx, monkeypatch):
    """验证研报构建器接收应用事件队列的非阻塞通知回调。

    参数：
        build_ctx: Callable，应用上下文异步构建夹具
        monkeypatch: MonkeyPatch，用于包装并观察研报构建函数

    返回：
        None，通过断言验证回调与事件队列 put_nowait 方法同源
    """
    captured: list = []

    def _wrap(settings, repo, audit, provider, notify_event=None, **kwargs):
        """包装真实 build_research：截获 notify_event 入参后转调真实实现。

        参数：
            settings: 应用配置，透传
            repo: 仓储对象，透传
            audit: 审计对象，透传
            provider: LLM provider，透传
            notify_event: 事件回调，被记录到 captured 列表供断言
            **kwargs: 其余透传参数

        返回：
            真实 build_research 的返回值（研报子系统组件）
        """
        captured.append(notify_event)
        return _build_research_impl(
            settings, repo, audit, provider, notify_event=notify_event, **kwargs
        )

    monkeypatch.setattr("src.bootstrap.build_research", _wrap)
    ctx = await build_ctx()
    assert captured == [ctx.event_queue.put_nowait]  # bound method 同 func+self 即相等


async def test_research_task_created_and_cancelled_on_shutdown(build_ctx, monkeypatch):
    """验证应用运行时创建研报巡检任务并在关闭流程中取消该任务。

    参数：
        build_ctx: Callable，应用上下文异步构建夹具
        monkeypatch: MonkeyPatch，用于替换长期任务与关闭流程

    返回：
        None，通过断言验证捕获到唯一且已取消的研报任务
    """
    ctx = await build_ctx()
    captured: list[asyncio.Task] = []

    async def _fake_forever() -> None:
        """替代研报调度器 run_forever：永久挂起，直至任务被取消。

        参数：无

        返回：
            None，永不自然返回；副作用是把协程挂起等待 cancel
        """
        await asyncio.Event().wait()  # 挂起直至被 cancel

    async def _fake_serve() -> None:
        """替代 server.serve：立即返回，避免占用真实 HTTP 端口。

        参数：无

        返回：
            None，立即完成（不启动真实 HTTP 服务）
        """
        await asyncio.sleep(0)

    async def _fake_shutdown(
        _ctx, server_task, pusher_task, funding_task, review_task, research_task, safety_task=None
    ) -> None:
        """替代 run_app 的 shutdown：记录研报任务并取消各后台任务。

        参数：
            _ctx: 应用上下文（本桩未使用）
            server_task: 服务端任务，被取消
            pusher_task: 行情推送任务（本桩不处理）
            funding_task: 资金费巡检任务，被取消
            review_task: 复盘巡检任务，被取消
            research_task: 研报巡检任务，记录到 captured 供断言其已被取消
            safety_task: 安全巡检任务（本桩不处理）

        返回：
            None，副作用是取消 server/funding/review/research 四个任务并等待其收尾
        """
        captured.append(research_task)
        for task in (server_task, funding_task, review_task, research_task):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            server_task, funding_task, review_task, research_task, return_exceptions=True
        )

    monkeypatch.setattr(ctx.research.scheduler, "run_forever", _fake_forever)
    monkeypatch.setattr(ctx.server, "serve", _fake_serve)  # 不起真实 HTTP 端口
    monkeypatch.setattr("src.bootstrap.shutdown", _fake_shutdown)

    await run_app(ctx, duration=0)
    assert len(captured) == 1
    assert captured[0].cancelled()


# ---------- 复盘子系统接线 ----------


async def test_review_subsystem_assembled(build_ctx):
    """验证复盘组件完成装配、播种初始版本并接入服务器写回调。

    参数：
        build_ctx: Callable，应用上下文异步构建夹具

    返回：
        None，通过断言验证组件、初始版本与三类服务器回调
    """
    ctx = await build_ctx()
    assert ctx.review.agent is not None
    assert ctx.review.scheduler is not None
    assert ctx.review.store is not None
    versions = await ctx.repo.review.list_strategy_versions()
    assert len(versions) == 1  # seed_if_empty 以真实 system_prompt.md 播种 v1
    assert versions[0].created_by == "human"
    deps = ctx.server_deps
    assert deps is not None
    assert deps.review_run == ctx.review.scheduler.run_now
    assert deps.strategy_save is not None
    assert deps.strategy_rollback is not None


async def test_strategy_save_rollback_callbacks(tmp_path: Path, build_ctx, monkeypatch):
    """验证策略保存与回滚回调的版本元数据、幂等、校验和文件回写语义。

    参数：
        tmp_path: Path，pytest 临时目录，用于隔离真实策略书
        build_ctx: Callable，应用上下文异步构建夹具
        monkeypatch: MonkeyPatch，用于把复盘根目录切换到临时目录

    返回：
        None，通过断言验证保存、重复保存、过短拒绝与历史回滚全链路
    """
    monkeypatch.setattr("src.review.setup.ROOT", tmp_path)  # 策略书/复盘提示词路径隔离
    (tmp_path / "system_prompt.md").write_text("初始策略书。" * 30, encoding="utf-8")
    ctx = await build_ctx()
    deps = ctx.server_deps
    assert deps is not None and deps.strategy_save is not None
    seeded = await ctx.repo.review.list_strategy_versions()
    assert len(seeded) == 1  # 播种 v1（隔离后的 tmp 策略书）

    new_content = "改进后的策略书。" * 30
    result = await deps.strategy_save(new_content)
    assert result == {"saved": True, "version": seeded[0].id + 1}
    version = await ctx.repo.review.get_strategy_version(result["version"])
    assert version is not None
    assert version.created_by == "human" and version.reason == "前端手动保存"
    assert (tmp_path / "system_prompt.md").read_text(encoding="utf-8") == new_content

    # 幂等：同内容重复保存不产新版本（仅"无差异"一条原因）
    assert await deps.strategy_save(new_content) == {"saved": True, "version": None}
    assert len(await ctx.repo.review.list_strategy_versions()) == 2
    # 过短：校验失败上抛 StrategyValidationError（server 路由映 422）
    with pytest.raises(StrategyValidationError):
        await deps.strategy_save("太短")

    assert deps.strategy_rollback is not None
    rolled = await deps.strategy_rollback(seeded[0].id)
    assert rolled["rolled_back_to"] == seeded[0].id
    assert rolled["version"] == seeded[0].id + 2  # 回滚也记新版本
    assert (tmp_path / "system_prompt.md").read_text(encoding="utf-8") == seeded[0].content


async def test_llm_reconfigure_rebuilds_each_agent_provider(tmp_path: Path, monkeypatch):
    """验证模型热重建为三个 Agent 分别创建新的独立 provider。

    参数：
        tmp_path: Path，pytest 临时目录，用于隔离应用数据库
        monkeypatch: MonkeyPatch，用于配置测试密钥并清除模拟模型开关

    返回：
        None，通过断言验证重建成功且三个 Agent 的 provider 均已独立替换
    """
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-key-for-reconfigure")
    monkeypatch.delenv("LLM_MOCK", raising=False)
    settings = Settings()
    settings.llm.provider = "openai_compat"
    ctx = await build_app(
        settings, WATCHLIST, mock_llm=False, mock_market=True, db_path=tmp_path / "t.db"
    )
    try:
        assert ctx.loop._provider is not None and ctx.review.agent._provider is not None
        assert ctx.research.agent._provider is not None
        assert ctx.review.agent._provider is not ctx.loop._provider  # 各自独立实例
        assert ctx.research.agent._provider is not ctx.loop._provider
        old_loop_provider = ctx.loop._provider
        old_review_provider = ctx.review.agent._provider
        old_research_provider = ctx.research.agent._provider
        deps = ctx.server_deps
        assert deps is not None and deps.llm_reconfigure is not None
        result = await deps.llm_reconfigure()
        assert result == {"llm_configured": True, "error": ""}
        assert ctx.loop._provider is not None and ctx.loop._provider is not old_loop_provider
        assert ctx.review.agent._provider is not None
        assert ctx.review.agent._provider is not old_review_provider
        assert ctx.research.agent._provider is not None
        assert ctx.research.agent._provider is not old_research_provider
    finally:
        await ctx.db.close()
