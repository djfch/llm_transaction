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
from httpx import ASGITransport, AsyncClient

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
from src.config_io import write_settings
from src.server.app import create_app

BTC = "BTC_USDT"
WATCHLIST = Watchlist(contracts=[BTC])


@pytest.fixture
def no_llm_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """模拟全新机器：环境中没有任何 LLM key / mock 标记。

    参数：
        monkeypatch: pytest.MonkeyPatch，用于隔离并替换依赖或环境变量的 pytest 夹具

    返回：
        None，执行上述模拟操作或副作用，无返回值
    """
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "LLM_MOCK"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
async def build_ctx(tmp_path: Path) -> AsyncIterator[Callable[..., AppContext]]:
    """build_app 工厂 + 统一清理（关闭数据库，避免 aiosqlite 线程跨用例泄漏）。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        AsyncIterator[Callable[..., AppContext]]，通过夹具向测试提供上述临时依赖，并在结束后清理资源
    """
    ctxs: list[AppContext] = []

    async def _factory(settings: Settings | None = None, **kwargs) -> AppContext:
        """调用 build_app 构造应用上下文并登记，供 fixture 收尾统一关闭数据库。

        参数：
            settings: Settings | None，应用配置；为 None 时使用默认 Settings()
            **kwargs: 透传给 build_app 的额外关键字参数（如 mock_llm）

        返回：
            AppContext：已构造并登记到清理列表的应用上下文
        """
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
    """无 LLM key：build_app 不崩，provider 降级 None，status_provider 透出 False。

    参数：
        no_llm_key: None，已清空 LLM 密钥的环境隔离夹具
        build_ctx: Callable[..., AppContext]，构建并自动清理应用上下文的夹具工厂

    返回：
        None，通过断言验证上述行为，无返回值
    """
    ctx = await build_ctx(mock_llm=False)
    assert ctx.loop.llm_configured is False
    deps = ctx.server_deps
    assert deps is not None and deps.status_provider is not None
    assert deps.status_provider()["llm_configured"] is False


async def test_run_once_without_provider_skips_round(no_llm_key, build_ctx):
    """provider=None：run_once 直接返回（不崩），不落审计、不计连续失败。

    参数：
        no_llm_key: None，已清空 LLM 密钥的环境隔离夹具
        build_ctx: Callable[..., AppContext]，构建并自动清理应用上下文的夹具工厂

    返回：
        None，通过断言验证上述行为，无返回值
    """
    ctx = await build_ctx(mock_llm=False)
    result = await ctx.loop.run_once("test_wake")
    assert result.ok is False
    assert result.error == "LLM 未配置"
    assert ctx.loop.consecutive_failures == 0
    assert await ctx.repo.latest_audit_round("paper") is None  # 不落审计


async def test_set_provider_hot_swap_marks_configured(no_llm_key, build_ctx):
    """set_provider 热替换后 llm_configured=True（status_provider 同步透出）。

    参数：
        no_llm_key: None，已清空 LLM 密钥的环境隔离夹具
        build_ctx: Callable[..., AppContext]，构建并自动清理应用上下文的夹具工厂

    返回：
        None，通过断言验证上述行为，无返回值
    """
    ctx = await build_ctx(mock_llm=False)
    ctx.loop.set_provider(MockProvider())
    assert ctx.loop.llm_configured is True
    assert ctx.server_deps is not None and ctx.server_deps.status_provider is not None
    assert ctx.server_deps.status_provider()["llm_configured"] is True


# ---------- llm_reconfigure 热重建接线 ----------


async def test_llm_reconfigure_keeps_old_provider_on_error(no_llm_key, build_ctx):
    """重建失败（仍无 key）：保留旧 provider（None），回报 llm_configured=False + error。

    参数：
        no_llm_key: None，已清空 LLM 密钥的环境隔离夹具
        build_ctx: Callable[..., AppContext]，构建并自动清理应用上下文的夹具工厂

    返回：
        None，通过断言验证上述行为，无返回值
    """
    ctx = await build_ctx(mock_llm=False)
    deps = ctx.server_deps
    assert deps is not None and deps.llm_reconfigure is not None
    result = await deps.llm_reconfigure()
    assert result["llm_configured"] is False
    assert result["error"]  # LLMError 原文透出
    assert ctx.loop.llm_configured is False  # 旧 provider 保留


async def test_llm_reconfigure_recovers_after_key_saved(no_llm_key, build_ctx, monkeypatch):
    """补齐 key 后重建成功：热替换生效，llm_configured=True。

    参数：
        no_llm_key: None，已清空 LLM 密钥的环境隔离夹具
        build_ctx: Callable[..., AppContext]，构建并自动清理应用上下文的夹具工厂
        monkeypatch: pytest.MonkeyPatch，用于隔离并替换依赖或环境变量的 pytest 夹具

    返回：
        None，通过断言验证上述行为，无返回值
    """
    ctx = await build_ctx(mock_llm=False)
    assert ctx.server_deps is not None and ctx.server_deps.llm_reconfigure is not None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
    result = await ctx.server_deps.llm_reconfigure()
    assert result == {"llm_configured": True, "error": ""}
    assert ctx.loop.llm_configured is True


async def test_status_keeps_active_trader_model_when_reconfigure_fails(
    no_dual_llm_key, build_ctx, monkeypatch, tmp_path
):
    """切换到缺少密钥的凭证失败后，状态接口继续展示实际生效的旧模型。

    参数：
        no_dual_llm_key: None，已清空多凭证密钥的环境隔离夹具
        build_ctx: Callable[..., AppContext]，构建并自动清理应用上下文的夹具工厂
        monkeypatch: pytest.MonkeyPatch，用于逐步补齐新凭证密钥
        tmp_path: Path，隔离公开配置与密钥写接口的临时目录

    返回：
        None，断言失败时保留旧模型，补齐密钥成功后才切换状态摘要
    """
    monkeypatch.setenv("LLM_KEY_MAIN", "sk-main")
    settings = _active_status_settings()
    config_path = tmp_path / "config.yaml"
    write_settings(settings.model_dump(), config_path)
    ctx = await build_ctx(settings, mock_llm=False)
    deps = ctx.server_deps
    assert deps is not None and deps.llm_reconfigure is not None
    deps.config_path = config_path
    deps.env_path = tmp_path / ".env"
    async with AsyncClient(
        transport=ASGITransport(app=create_app(deps)), base_url="http://test"
    ) as client:
        config = (await client.get("/api/config")).json()
        config["agents"]["trader"]["credential"] = "broken"
        changed = await client.put("/api/config", json=config)
        assert changed.status_code == 200
        assert changed.json()["llm_configured"] is True
        assert "trader" in changed.json()["llm_error"]
        _assert_llm_status(
            (await client.get("/api/status")).json(),
            name="main",
            provider="anthropic",
            model="deepseek-v4-flash",
            effort="",
        )

        repaired = await client.post(
            "/api/secrets", json={"credential": "broken", "api_key": "sk-fixed"}
        )
        assert repaired.json() == {"saved": True, "llm_configured": True, "error": ""}
        _assert_llm_status(
            (await client.get("/api/status")).json(),
            name="broken",
            provider="openai_compat",
            model="deepseek-v4-pro",
            effort="high",
        )


async def test_llm_reconfigure_short_circuits_in_mock(build_ctx):
    """mock_llm：直接回报已配置（不重建真实 provider）。

    参数：
        build_ctx: Callable[..., AppContext]，构建并自动清理应用上下文的夹具工厂

    返回：
        None，通过断言验证上述行为，无返回值
    """
    ctx = await build_ctx(mock_llm=True)
    assert ctx.server_deps is not None and ctx.server_deps.llm_reconfigure is not None
    result = await ctx.server_deps.llm_reconfigure()
    assert result == {"llm_configured": True, "error": ""}


async def test_skip_round_still_drains_fills(no_llm_key, build_ctx):
    """provider=None 跳轮也先泄放成交缓冲，避免未配置 LLM 时成交滞留。

    参数：
        no_llm_key: None，已清空 LLM 密钥的环境隔离夹具
        build_ctx: Callable[..., AppContext]，构建并自动清理应用上下文的夹具工厂

    返回：
        None，通过断言验证上述行为，无返回值
    """
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
    """构造分别为决策与复盘 agent 指定独立模型凭证的测试配置。

    参数：无

    返回：
        Settings，决策 agent 使用 main 凭证、复盘 agent 使用 review 凭证的配置
    """
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
            researcher=AgentBinding(credential="main"),  # 研报 agent 复用主号
        ),
    )


def _active_status_settings() -> Settings:
    """构造可验证旧模型保留与新模型成功切换的双凭证配置。

    参数：无

    返回：
        Settings，三个 Agent 初始共用 main，broken 凭证等待切换
    """
    settings = _dual_settings()
    settings.llm.credentials[0].model = "deepseek-v4-flash"
    settings.llm.credentials.append(
        CredentialConfig(
            name="broken",
            provider="openai_compat",
            model="deepseek-v4-pro",
            thinking_effort="high",
            api_key_env="LLM_KEY_BROKEN",
        )
    )
    settings.agents.reviewer.credential = "main"
    return settings


def _assert_llm_status(status: dict, *, name: str, provider: str, model: str, effort: str) -> None:
    """断言状态接口完整返回实际生效的 trader 凭证摘要。

    参数：
        status: dict，GET /api/status 的 JSON 响应
        name: str，预期凭证名称
        provider: str，预期提供商
        model: str，预期模型名称
        effort: str，预期思考强度

    返回：
        None，通过断言冻结凭证四字段及已配置状态
    """
    assert status["llm_credential_name"] == name
    assert status["llm_provider"] == provider
    assert status["llm_model"] == model
    assert status["llm_thinking_effort"] == effort
    assert status["llm_configured"] is True


@pytest.fixture
def no_dual_llm_key(no_llm_key, monkeypatch: pytest.MonkeyPatch) -> None:
    """在 no_llm_key 基础上再清掉多凭证环境变量。

    参数：
        no_llm_key: None，已清空 LLM 密钥的环境隔离夹具
        monkeypatch: pytest.MonkeyPatch，用于隔离并替换依赖或环境变量的 pytest 夹具

    返回：
        None，执行上述模拟操作或副作用，无返回值
    """
    for name in ("LLM_KEY_MAIN", "LLM_KEY_REVIEW", "LLM_KEY_BROKEN"):
        monkeypatch.delenv(name, raising=False)


async def test_dual_credentials_built_per_agent(no_dual_llm_key, build_ctx, monkeypatch):
    """双凭证：两个 agent 按其绑定凭证各自构造 provider（独立实例、独立降级）。

    参数：
        no_dual_llm_key: None，已清空双凭证密钥的环境隔离夹具
        build_ctx: Callable[..., AppContext]，构建并自动清理应用上下文的夹具工厂
        monkeypatch: pytest.MonkeyPatch，用于隔离并替换依赖或环境变量的 pytest 夹具

    返回：
        None，通过断言验证上述行为，无返回值
    """
    monkeypatch.setenv("LLM_KEY_MAIN", "sk-main")
    ctx = await build_ctx(_dual_settings(), mock_llm=False)
    assert ctx.loop.llm_configured is True  # trader 凭证有 key
    assert ctx.review.agent._provider is None  # reviewer 凭证缺 key：独立降级，不影响决策循环
    assert ctx.review.agent._provider is not ctx.loop._provider


async def test_dual_reconfigure_failure_names_agent_and_keeps_other(
    no_dual_llm_key, build_ctx, monkeypatch
):
    """单 agent 重建失败：error 点名该 agent 与环境变量，另一个 agent 不受影响。

    参数：
        no_dual_llm_key: None，已清空双凭证密钥的环境隔离夹具
        build_ctx: Callable[..., AppContext]，构建并自动清理应用上下文的夹具工厂
        monkeypatch: pytest.MonkeyPatch，用于隔离并替换依赖或环境变量的 pytest 夹具

    返回：
        None，通过断言验证上述行为，无返回值
    """
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
    """mock 模式行为不变：两个 agent 共享同一 MockProvider，reconfigure 短路已配置。

    参数：
        build_ctx: Callable[..., AppContext]，构建并自动清理应用上下文的夹具工厂

    返回：
        None，通过断言验证上述行为，无返回值
    """
    ctx = await build_ctx(_dual_settings(), mock_llm=True)
    assert isinstance(ctx.loop._provider, MockProvider)
    assert ctx.review.agent._provider is ctx.loop._provider
    assert ctx.server_deps is not None and ctx.server_deps.llm_reconfigure is not None
    result = await ctx.server_deps.llm_reconfigure()
    assert result == {"llm_configured": True, "error": ""}
