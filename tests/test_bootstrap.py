"""bootstrap 接线回归测试：DecisionLoop 依赖注入、server runtime 同步、资金费周期、预警生命周期。

覆盖第三波修复的接线层缺陷：
- paper 模式注入 drain_fills；真实网关为 None（工具层 inline 落 trade，二者互斥）
- persist_kill_switch 写回 config.yaml；audit 与 server 共用同一实例
- ServerDeps.runtime_settings / runtime_watchlist 与决策循环共享同一对象
- 资金费按合约 funding_interval 结算（8h 周期，1 小时内不重复结算）
- 预警线：触发即持久化关闭 alerts 行；重启后从 DB 重建 TriggerManager
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from src.bootstrap import AppContext, _default_contract, build_app, settle_due_funding
from src.config import Settings, Watchlist
from src.config_io import write_settings
from src.gateway.base import OrderRequest, Ticker
from src.memory.db import Database
from src.memory.repo import Repo
from src.paper.engine import PaperGateway

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
    """paper 模式：drain_fills 接 PaperGateway.drain_fills，工具层不再 inline 落库。"""
    ctx = await build_ctx()
    assert isinstance(ctx.gateway, PaperGateway)
    assert ctx.loop._drain_fills == ctx.gateway.drain_fills  # bound method 同 func+self 即相等
    assert ctx.loop._deps.save_fills_inline is False


async def test_real_gateway_mode_has_no_drain_fills(build_ctx):
    """真实网关（testnet）：无 drain_fills 钩子，工具层下单时 inline 落 trade。"""
    ctx = await build_ctx(Settings(mode="testnet"))
    assert ctx.loop._drain_fills is None
    assert ctx.loop._deps.save_fills_inline is True


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


# ---------- 预警线生命周期 ----------


async def test_alert_deactivated_after_trigger_fires(build_ctx):
    """触发即持久化关闭：触发后 alerts 行 active=0（不再永久悬挂）。"""
    ctx = await build_ctx()
    await ctx.repo.add_alert("r1", BTC, "above", Decimal("60000"))
    ctx.triggers.add(BTC, ">=", Decimal("60000"))
    await ctx.source.push_ticker(_ticker(Decimal("60000")))  # type: ignore[attr-defined]

    async def _poll() -> None:  # deactivate 在后台任务中异步落库，轮询等待
        while (await ctx.repo.list_alerts(active_only=False))[0].active:
            await asyncio.sleep(0)

    await asyncio.wait_for(_poll(), 2)
    assert (await ctx.repo.list_alerts(active_only=False))[0].active is False


async def test_alerts_rebuilt_from_db_on_startup(tmp_path: Path, build_ctx):
    """启动重建：DB 中 active=1 的告警恢复为内存触发器（重启后预警不丢）。"""
    db = Database()
    await db.open(tmp_path / "t.db")  # 与 _build 相同的 db_path：预置告警行
    repo = Repo(db)
    await repo.add_alert("r1", BTC, "above", Decimal("60000"))
    gone = await repo.add_alert("r2", "ETH_USDT", "below", Decimal("3000"))
    await repo.deactivate_alert(gone.id)  # 已关闭的不应重建
    await db.close()

    ctx = await build_ctx()
    triggers = ctx.triggers.list()
    assert len(triggers) == 1
    assert triggers[0].contract == BTC
    assert triggers[0].direction == ">="  # above → >=
    assert triggers[0].price == Decimal("60000")


# ---------- server 写操作接线（监控界面改进） ----------


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
