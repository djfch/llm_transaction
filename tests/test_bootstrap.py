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

from src.bootstrap import AppContext, _default_contract, build_app
from src.config import Settings, Watchlist
from src.config_io import write_settings
from src.gateway.base import OrderRequest, Ticker
from src.paper.engine import PaperGateway
from src.paper.funding_patrol import settle_due_funding
from src.review.strategy import StrategyValidationError

BTC = "BTC_USDT"
WATCHLIST = Watchlist(contracts=[BTC])


@pytest.fixture
async def build_ctx(tmp_path: Path) -> AsyncIterator[Callable[..., AppContext]]:
    """build_app 工厂 + 统一清理（关闭数据库，避免 aiosqlite 线程跨用例泄漏）。"""
    ctxs: list[AppContext] = []

    async def _factory(settings: Settings | None = None, **kwargs) -> AppContext:
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
    """paper 模式：drain_fills 接 PaperGateway.drain_fills；不建私有成交订阅。"""
    ctx = await build_ctx()
    assert isinstance(ctx.gateway, PaperGateway)
    assert ctx.loop._drain_fills == ctx.gateway.drain_fills  # bound method 同 func+self 即相等
    assert ctx.trade_feed is None


async def test_real_gateway_mode_has_no_drain_fills(build_ctx):
    """真实网关（testnet）：无 drain_fills 钩子；mock_market 下不建私有成交订阅。"""
    ctx = await build_ctx(Settings(mode="testnet"))
    assert ctx.loop._drain_fills is None
    assert ctx.trade_feed is None  # mock_market=True 时不接私有 WS（真实装配见 build_app）


async def test_persist_kill_switch_writes_config(tmp_path: Path, build_ctx):
    """风控锁经注入回调写回 config.yaml（risk.kill_switch=true）。"""
    config_path = tmp_path / "config.yaml"
    write_settings({}, config_path)  # 先落一份合法配置
    ctx = await build_ctx(config_path=config_path)
    ctx.loop._persist_kill_switch(True)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert raw["risk"]["kill_switch"] is True


async def test_audit_shared_between_loop_and_server(build_ctx):
    """DecisionLoop 与 server 共用同一 AuditTrail 实例（审计单点落库）。"""
    ctx = await build_ctx()
    assert ctx.server_deps is not None
    assert ctx.loop._audit is ctx.server_deps.audit_trail


# ---------- server runtime 同步 ----------


async def test_server_runtime_shares_settings_and_watchlist(build_ctx):
    """runtime_settings 是同一 Settings 实例；runtime_watchlist 是同一 list 对象。"""
    ctx = await build_ctx()
    deps = ctx.server_deps
    assert deps is not None
    assert deps.runtime_settings is ctx.settings
    assert deps.runtime_watchlist is ctx.watchlist
    assert ctx.loop._deps.watchlist is ctx.watchlist  # 决策循环持有的也是同一 list


# ---------- 资金费按 funding_interval 结算 ----------


def _paper_with_long_position() -> PaperGateway:
    """持有多单的 paper 网关（合约 funding_interval=28800s，费率 0.0001）。"""
    gateway = PaperGateway(Settings().paper)
    gateway.upsert_contract(_default_contract(BTC, Decimal("50000")))
    gateway.on_price(BTC, Decimal("50000"))
    gateway.place_order(OrderRequest(contract=BTC, size=Decimal(1)))
    return gateway


def test_funding_settles_only_when_interval_due():
    """8h 结算周期：1 小时内不重复结算，到达周期才再次结算（修约 8 倍高估）。"""
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
    """触发即一次性从内存索引移除，并以 price_trigger 原因抢醒调度器。"""
    ctx = await build_ctx()
    ctx.triggers.add(BTC, ">=", Decimal("60000"))
    wakes: list[str] = []
    orig_wake_now = ctx.scheduler.wake_now
    ctx.scheduler.wake_now = lambda reason: wakes.append(reason) or orig_wake_now(reason)

    await ctx.source.push_ticker(_ticker(Decimal("60000")))  # type: ignore[attr-defined]

    assert ctx.triggers.list() == []  # 一次性语义：触发即移除
    assert wakes == [f"price_trigger:{BTC}@60000"]


async def test_triggers_not_rebuilt_on_restart(build_ctx):
    """内存唯一存储：重启（重新组装）后预警线不重建，索引为空，由 LLM 决定是否重设。"""
    ctx = await build_ctx()
    ctx.triggers.add(BTC, ">=", Decimal("60000"))
    assert len(ctx.triggers.list()) == 1
    ctx2 = await build_ctx()  # 同一 db_path 重新组装 = 模拟进程重启：内存索引全新
    assert ctx2.triggers.list() == []


# ---------- server 写操作接线 ----------


async def test_server_deps_wires_trading_callbacks(build_ctx):
    """manual_close / agent_start / agent_stop 已注入；paper 模式注入 paper_reset。"""
    ctx = await build_ctx()
    deps = ctx.server_deps
    assert deps is not None
    assert callable(deps.manual_close)
    assert deps.agent_start is not None and deps.agent_stop is not None
    assert deps.paper_reset is not None  # paper 模式接 PaperGateway.reset_account


async def test_server_deps_no_paper_reset_in_testnet(build_ctx):
    """testnet（真实网关）不注入 paper_reset，/api/paper/reset 将 409。"""
    ctx = await build_ctx(Settings(mode="testnet"))
    assert ctx.server_deps is not None
    assert ctx.server_deps.paper_reset is None


async def test_agent_start_stop_callbacks_drive_scheduler(build_ctx):
    """agent_start/stop 回调真实启停调度器，status_provider 反映 agent_running。"""
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
    """手动启动立即抢醒首轮（而非干等 default_wake_minutes 后的首个定时唤醒）。"""
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
    """status_provider 暴露 in_round 键（未运行时为 False），供 /api/agent/live 实时展示。"""
    ctx = await build_ctx()
    deps = ctx.server_deps
    assert deps is not None and deps.status_provider is not None
    status = deps.status_provider()
    assert "in_round" in status
    assert status["in_round"] is False


async def test_on_wake_pushes_round_start_then_round(build_ctx):
    """决策轮事件序：round_start（轮开始）先于 round（轮结束）入队——
    前端实时决策卡依靠 round_start 进入"决策中"轮询态。"""
    ctx = await build_ctx()  # fixture 默认 mock_llm + mock_market
    await ctx.scheduler.start()
    try:
        ctx.scheduler.wake_now("test_event_order")
        first = await asyncio.wait_for(ctx.event_queue.get(), timeout=5)
        second = await asyncio.wait_for(ctx.event_queue.get(), timeout=5)
    finally:
        await ctx.scheduler.stop()
    assert first["type"] == "round_start"
    assert first["data"]["wake_source"] == "test_event_order"
    assert second["type"] == "round"


# ---------- 复盘子系统接线 ----------


async def test_review_subsystem_assembled(build_ctx):
    """复盘子系统装配进 AppContext：版本库播种 v1，server 三个复盘写回调全部接线。"""
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
    """strategy_save 回调：经 StrategyStore 落版本（created_by='human'，reason 固定）；
    纯"无差异"幂等成功（version=None）；其余校验失败上抛（路由映 422）；
    strategy_rollback 回写历史内容并记新版本。ROOT 隔离到 tmp，不动真实策略书。"""
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
    """LLM 热重建后，决策循环与复盘 agent 各自换到新 provider（按 agent 独立实例）。"""
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-key-for-reconfigure")
    monkeypatch.delenv("LLM_MOCK", raising=False)
    settings = Settings()
    settings.llm.provider = "openai_compat"
    ctx = await build_app(
        settings, WATCHLIST, mock_llm=False, mock_market=True, db_path=tmp_path / "t.db"
    )
    try:
        assert ctx.loop._provider is not None and ctx.review.agent._provider is not None
        assert ctx.review.agent._provider is not ctx.loop._provider  # 各自独立实例
        old_loop_provider, old_review_provider = ctx.loop._provider, ctx.review.agent._provider
        deps = ctx.server_deps
        assert deps is not None and deps.llm_reconfigure is not None
        result = await deps.llm_reconfigure()
        assert result == {"llm_configured": True, "error": ""}
        assert ctx.loop._provider is not None and ctx.loop._provider is not old_loop_provider
        assert ctx.review.agent._provider is not None
        assert ctx.review.agent._provider is not old_review_provider  # 两边都热替换
    finally:
        await ctx.db.close()
