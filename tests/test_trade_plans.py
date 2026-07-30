"""交易计划功能测试：PlansRepo 存取 / 工具 save+close / 上下文注入 / GET /api/plans。

计划为建议性记录：不下单、不经风控（工具结果 risk_verdict 为空串），
每合约至多一个 active 计划由 save_plan 的替代语义保证。
"""

import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from src.agent.context import ContextBuilder
from src.agent.tool_handlers import ToolDeps
from src.agent.tools import ToolRegistry
from src.audit.trail import AuditTrail
from src.config import AuditConfig
from src.config_io import write_settings
from src.gateway.mock import MockGateway
from src.market.candles import CandleCache, ManualPriceSource
from src.market.triggers import TriggerManager
from src.memory import Database, Repo
from src.server.app import create_app
from src.server.deps import ServerDeps


@pytest.fixture
async def repo(tmp_path):
    db = Database()
    await db.open(tmp_path / "plans.db")
    yield Repo(db)
    await db.close()


def _plan_args(contract: str = "BTC_USDT", **extra) -> dict:
    args = {
        "contract": contract,
        "direction": "short",
        "entry": "反弹至 64200-64300 受阻",
        "stop_loss": "64500",
        "take_profit": "63800→63666",
        "condition": "15m 转阴且量能萎缩",
    }
    args.update(extra)
    return args


async def _save(repo: Repo, contract: str = "BTC_USDT", round_id: str = "r1"):
    return await repo.plans.save_plan(
        round_id=round_id,
        contract=contract,
        direction="short",
        entry="64200-64300",
        stop_loss="64500",
        take_profit="63800",
        condition="15m 转阴",
    )


# ---------- PlansRepo ----------


async def test_save_and_get_plan(repo: Repo):
    plan = await _save(repo)
    assert plan.id > 0 and plan.status == "active"
    loaded = await repo.plans.get_plan(plan.id)
    assert loaded is not None and loaded.contract == "BTC_USDT"
    assert await repo.plans.get_plan(9999) is None


async def test_one_active_per_contract(repo: Repo):
    """同合约再立计划：旧 active 自动置 cancelled（被新计划替代）；异合约互不影响。"""
    old = await _save(repo, "BTC_USDT")
    other = await _save(repo, "ETH_USDT")
    new = await _save(repo, "BTC_USDT", round_id="r2")

    replaced = await repo.plans.get_plan(old.id)
    assert replaced is not None and replaced.status == "cancelled"
    assert replaced.closed_reason == "被新计划替代"
    active = await repo.plans.active_plans()
    assert {p.id for p in active} == {other.id, new.id}


async def test_close_plan_only_active(repo: Repo):
    plan = await _save(repo)
    closed = await repo.plans.close_plan(plan.id, "executed", "已按计划入场")
    assert closed is not None and closed.status == "executed"
    assert closed.closed_reason == "已按计划入场"
    # 再关一次 / 关不存在的：返回 None
    assert await repo.plans.close_plan(plan.id, "cancelled", "x") is None
    assert await repo.plans.close_plan(9999, "cancelled", "x") is None


async def test_list_plans_page_and_status_filter(repo: Repo):
    for i in range(3):
        await _save(repo, f"C{i}_USDT")
    await repo.plans.close_plan(1, "executed", "done")

    all_page, total = await repo.plans.list_plans_page(limit=2, offset=0)
    assert total == 3 and [p.id for p in all_page] == [3, 2]  # 最新在前
    active, active_total = await repo.plans.list_plans_page(limit=10, offset=0, status="active")
    assert active_total == 2 and all(p.status == "active" for p in active)
    executed, executed_total = await repo.plans.list_plans_page(
        limit=10, offset=0, status="executed"
    )
    assert executed_total == 1 and executed[0].id == 1


# ---------- 工具层（registry.execute，不经风控） ----------


def _tool_deps(repo: Repo) -> ToolDeps:
    """计划工具只用 repo 与 round_id：其余依赖空占位。"""
    none = SimpleNamespace()
    return ToolDeps(
        gateway=none,
        risk_engine=none,
        risk_config=none,
        watchlist=[],
        repo=repo,
        candles=none,
        triggers=none,
        daily_stats_fn=None,
        round_id="r-tool",
    )


async def test_tool_save_and_close(repo: Repo):
    registry = ToolRegistry(_tool_deps(repo))
    out = await registry.execute("save_trade_plan", _plan_args())
    assert "交易计划已保存" in out.text and "plan_id=1" in out.text
    assert out.risk_verdict == ""  # 纯记录工具不经风控

    out = await registry.execute(
        "close_trade_plan", {"plan_id": 1, "outcome": "executed", "reason": "条件满足已入场"}
    )
    assert "已标记为已执行" in out.text
    plan = await repo.plans.get_plan(1)
    assert plan is not None and plan.status == "executed" and plan.round_id == "r-tool"


async def test_tool_validation_errors(repo: Repo):
    registry = ToolRegistry(_tool_deps(repo))
    out = await registry.execute("save_trade_plan", _plan_args(direction="up"))
    assert "参数错误" in out.text  # direction 枚举
    out = await registry.execute("save_trade_plan", {"contract": "BTC_USDT"})
    assert "参数错误" in out.text  # 缺必填
    out = await registry.execute("save_trade_plan", _plan_args(valid_hours=721))
    assert "参数错误" in out.text and "valid_hours" in out.text
    out = await registry.execute(
        "close_trade_plan", {"plan_id": 42, "outcome": "executed", "reason": "x"}
    )
    assert "未找到 active 状态的计划" in out.text
    out = await registry.execute("close_trade_plan", {"outcome": "executed", "reason": "x"})
    assert "参数错误" in out.text  # 缺 plan_id


async def test_tool_valid_hours_sets_expiry(repo: Repo):
    registry = ToolRegistry(_tool_deps(repo))
    await registry.execute("save_trade_plan", _plan_args(valid_hours=2))
    plan = await repo.plans.get_plan(1)
    assert plan is not None and plan.expires_at is not None
    assert 1.9 * 3600 < plan.expires_at - time.time() <= 2 * 3600


# ---------- 上下文注入 ----------


async def _context_text(repo: Repo) -> str:
    gateway = MockGateway()
    candles = CandleCache(gateway, ManualPriceSource())
    builder = ContextBuilder(
        gateway, repo, candles, TriggerManager(lambda t, p: None), ["BTC_USDT"]
    )
    return (await builder.build("timer")).text


async def test_context_plans_empty(repo: Repo):
    text = await _context_text(repo)
    assert "## 交易计划（active 0 条" in text


async def test_context_plans_rendered_with_expired_tag(repo: Repo):
    await _save(repo, "BTC_USDT")
    expired = await repo.plans.save_plan(
        round_id="r1",
        contract="ETH_USDT",
        direction="long",
        entry="1900",
        stop_loss="1880",
        take_profit="1950",
        condition="回踩确认",
        expires_at=time.time() - 60,
    )
    text = await _context_text(repo)
    assert "## 交易计划（active 2 条" in text
    assert "[plan_id=1] BTC_USDT 做空：入场 64200-64300，止损 64500" in text
    assert f"[plan_id={expired.id}] ETH_USDT 做多" in text
    assert "［已过期，请更新或关闭］" in text
    # 未过期计划不带过期标注
    assert text.count("［已过期，请更新或关闭］") == 1


# ---------- GET /api/plans ----------


@pytest.fixture
async def client(repo: Repo, tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    write_settings({}, config_path)
    watchlist_path = tmp_path / "watchlist.yaml"
    watchlist_path.write_text(
        yaml.safe_dump({"settle": "usdt", "contracts": ["BTC_USDT"]}), encoding="utf-8"
    )
    prompt_path = tmp_path / "system_prompt.md"
    prompt_path.write_text("提示词", encoding="utf-8")
    deps = ServerDeps(
        repo=repo,
        audit_trail=AuditTrail(repo, AuditConfig(dir=str(tmp_path / "audit"))),
        gateway=MockGateway(),
        status_provider=lambda: {"uptime_seconds": 1},
        config_path=config_path,
        watchlist_path=watchlist_path,
        prompt_path=prompt_path,
        web_dist=tmp_path / "no_dist",
    )
    app = create_app(deps)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_api_plans_pagination_and_filter(client: AsyncClient, repo: Repo):
    for i in range(3):
        await _save(repo, f"C{i}_USDT")
    await repo.plans.close_plan(2, "cancelled", "放弃")

    r = await client.get("/api/plans", params={"limit": 2, "offset": 0})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3 and len(body["items"]) == 2
    assert body["items"][0]["id"] == 3  # 最新在前
    assert body["items"][0]["status"] == "active"

    r = await client.get("/api/plans", params={"status": "cancelled"})
    body = r.json()
    assert body["total"] == 1 and body["items"][0]["closed_reason"] == "放弃"

    r = await client.get("/api/plans", params={"status": "bogus"})
    assert r.status_code == 422  # 非法状态枚举
