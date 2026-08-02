"""LLM 启动韧性与热重建测试：缺 key 不崩、provider 可空、set_provider 热替换。

覆盖：
- 无 LLM key 时 build_app 不崩（provider 降级 None），status_provider 透出 llm_configured=False
- provider=None 时 run_once 跳过本轮（不落审计、不计连续失败）
- set_provider 热替换后 llm_configured=True
- ServerDeps.llm_reconfigure：缺 key 保留旧 provider 并回报 error；补 key 后重建成功
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from src.agent.providers.mock import MockProvider
from src.bootstrap import AppContext, build_app
from src.config import (
    AgentBinding,
    AgentsConfig,
    CredentialConfig,
    LLMConfig,
    Settings,
    Watchlist,
)

BTC = "BTC_USDT"
WATCHLIST = Watchlist(contracts=[BTC])


@pytest.fixture
def no_llm_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """模拟全新机器：环境中没有任何 LLM key / mock 标记。"""
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "LLM_MOCK"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
async def build_ctx(tmp_path: Path) -> AsyncIterator[Callable[..., AppContext]]:
    """build_app 工厂 + 统一清理（关闭数据库，避免 aiosqlite 线程跨用例泄漏）。"""
    ctxs: list[AppContext] = []

    async def _factory(settings: Settings | None = None, **kwargs) -> AppContext:
        ctx = await build_app(
            settings or Settings(),
            WATCHLIST,
            mock_market=True,
            db_path=tmp_path / "t.db",
            **kwargs,
        )
        ctxs.append(ctx)
        return ctx

    yield _factory
    for ctx in ctxs:
        await ctx.db.close()


# ---------- 启动韧性：缺 key 不崩 ----------


async def test_build_app_without_llm_key_degrades_gracefully(no_llm_key, build_ctx):
    """无 LLM key：build_app 不崩，provider 降级 None，status_provider 透出 False。"""
    ctx = await build_ctx(mock_llm=False)
    assert ctx.loop.llm_configured is False
    deps = ctx.server_deps
    assert deps is not None and deps.status_provider is not None
    assert deps.status_provider()["llm_configured"] is False


async def test_run_once_without_provider_skips_round(no_llm_key, build_ctx):
    """provider=None：run_once 直接返回（不崩），不落审计、不计连续失败。"""
    ctx = await build_ctx(mock_llm=False)
    result = await ctx.loop.run_once("test_wake")
    assert result.ok is False
    assert result.error == "LLM 未配置"
    assert ctx.loop.consecutive_failures == 0
    assert await ctx.repo.latest_audit_round("paper") is None  # 不落审计


async def test_set_provider_hot_swap_marks_configured(no_llm_key, build_ctx):
    """set_provider 热替换后 llm_configured=True（status_provider 同步透出）。"""
    ctx = await build_ctx(mock_llm=False)
    ctx.loop.set_provider(MockProvider())
    assert ctx.loop.llm_configured is True
    assert ctx.server_deps is not None and ctx.server_deps.status_provider is not None
    assert ctx.server_deps.status_provider()["llm_configured"] is True


# ---------- llm_reconfigure 热重建接线 ----------


async def test_llm_reconfigure_keeps_old_provider_on_error(no_llm_key, build_ctx):
    """重建失败（仍无 key）：保留旧 provider（None），回报 llm_configured=False + error。"""
    ctx = await build_ctx(mock_llm=False)
    deps = ctx.server_deps
    assert deps is not None and deps.llm_reconfigure is not None
    result = await deps.llm_reconfigure()
    assert result["llm_configured"] is False
    assert result["error"]  # LLMError 原文透出
    assert ctx.loop.llm_configured is False  # 旧 provider 保留


async def test_llm_reconfigure_recovers_after_key_saved(no_llm_key, build_ctx, monkeypatch):
    """补齐 key 后重建成功：热替换生效，llm_configured=True。"""
    ctx = await build_ctx(mock_llm=False)
    assert ctx.server_deps is not None and ctx.server_deps.llm_reconfigure is not None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
    result = await ctx.server_deps.llm_reconfigure()
    assert result == {"llm_configured": True, "error": ""}
    assert ctx.loop.llm_configured is True


async def test_llm_reconfigure_short_circuits_in_mock(build_ctx):
    """mock_llm：直接回报已配置（不重建真实 provider）。"""
    ctx = await build_ctx(mock_llm=True)
    assert ctx.server_deps is not None and ctx.server_deps.llm_reconfigure is not None
    result = await ctx.server_deps.llm_reconfigure()
    assert result == {"llm_configured": True, "error": ""}


async def test_skip_round_still_drains_fills(no_llm_key, build_ctx):
    """provider=None 跳轮也先泄放成交缓冲，避免未配置 LLM 时成交滞留。"""
    from decimal import Decimal

    from src.gateway.base import OrderRequest, Ticker

    ctx = await build_ctx(mock_llm=False)
    ticker = Ticker(
        contract=BTC,
        last=Decimal("60000"),
        mark_price=Decimal("60000"),
        funding_rate=Decimal("0.0001"),
        high_24h=Decimal("60000"),
        low_24h=Decimal("60000"),
        change_percentage=Decimal("0.5"),
    )
    await ctx.source.push_ticker(ticker)  # type: ignore[attr-defined]  # ManualPriceSource
    result = ctx.gateway.place_order(OrderRequest(contract=BTC, size=Decimal(1)))
    assert result.status == "finished"

    await ctx.loop.run_once("test_wake")  # provider=None → 跳轮

    trades = await ctx.repo.list_trades()
    assert len(trades) == 1, "跳轮前成交缓冲必须先泄放落库"
    assert trades[0].source == "llm_open"


# ---------- 多凭证：双 agent 独立构造与热重建 ----------


def _dual_settings() -> Settings:
    """trader→main（anthropic / LLM_KEY_MAIN），reviewer→review（openai_compat / LLM_KEY_REVIEW）。"""
    return Settings(
        llm=LLMConfig(
            credentials=[
                CredentialConfig(
                    name="main",
                    provider="anthropic",
                    model="claude-sonnet-4-5",
                    api_key_env="LLM_KEY_MAIN",
                ),
                CredentialConfig(
                    name="review",
                    provider="openai_compat",
                    model="deepseek-v4-flash",
                    api_key_env="LLM_KEY_REVIEW",
                ),
            ]
        ),
        agents=AgentsConfig(
            trader=AgentBinding(credential="main"),
            reviewer=AgentBinding(credential="review"),
        ),
    )


@pytest.fixture
def no_dual_llm_key(no_llm_key, monkeypatch: pytest.MonkeyPatch) -> None:
    """在 no_llm_key 基础上再清掉多凭证环境变量。"""
    for name in ("LLM_KEY_MAIN", "LLM_KEY_REVIEW"):
        monkeypatch.delenv(name, raising=False)


async def test_dual_credentials_built_per_agent(no_dual_llm_key, build_ctx, monkeypatch):
    """双凭证：两个 agent 按其绑定凭证各自构造 provider（独立实例、独立降级）。"""
    monkeypatch.setenv("LLM_KEY_MAIN", "sk-main")
    ctx = await build_ctx(_dual_settings(), mock_llm=False)
    assert ctx.loop.llm_configured is True  # trader 凭证有 key
    assert ctx.review.agent._provider is None  # reviewer 凭证缺 key：独立降级，不影响决策循环
    assert ctx.review.agent._provider is not ctx.loop._provider


async def test_dual_reconfigure_failure_names_agent_and_keeps_other(
    no_dual_llm_key, build_ctx, monkeypatch
):
    """单 agent 重建失败：error 点名该 agent 与环境变量，另一个 agent 不受影响。"""
    monkeypatch.setenv("LLM_KEY_MAIN", "sk-main")
    ctx = await build_ctx(_dual_settings(), mock_llm=False)
    assert ctx.server_deps is not None and ctx.server_deps.llm_reconfigure is not None
    old_loop_provider = ctx.loop._provider
    result = await ctx.server_deps.llm_reconfigure()
    assert result["llm_configured"] is True  # trader 正常重建
    assert "reviewer" in result["error"] and "LLM_KEY_REVIEW" in result["error"]  # 点名失败方
    assert ctx.loop._provider is not old_loop_provider  # trader 已热替换
    assert ctx.review.agent._provider is None  # reviewer 失败保留旧 provider

    monkeypatch.setenv("LLM_KEY_REVIEW", "sk-review")  # 补齐后全部重建成功
    result = await ctx.server_deps.llm_reconfigure()
    assert result == {"llm_configured": True, "error": ""}
    assert ctx.review.agent._provider is not None


async def test_mock_mode_shares_mock_provider(build_ctx):
    """mock 模式行为不变：两个 agent 共享同一 MockProvider，reconfigure 短路已配置。"""
    ctx = await build_ctx(_dual_settings(), mock_llm=True)
    assert isinstance(ctx.loop._provider, MockProvider)
    assert ctx.review.agent._provider is ctx.loop._provider
    assert ctx.server_deps is not None and ctx.server_deps.llm_reconfigure is not None
    result = await ctx.server_deps.llm_reconfigure()
    assert result == {"llm_configured": True, "error": ""}
