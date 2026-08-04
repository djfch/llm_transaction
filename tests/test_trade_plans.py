"""交易计划功能测试：PlansRepo 单行存取 / 工具 update+clear / 上下文注入 / GET /api/plan。

设计口径：全局唯一一份自由文本计划，更新即全文覆盖；空串 = 无计划；
纯记录工具不经风控（risk_verdict 为空串）；历史不留表（审计快照已冻结当轮原文）。
"""

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

_PLAN_MD = "## BTC 做空\n入场：反弹 64200-64300 受阻；止损 64500；目标 63800\n## ETH\n观望"


@pytest.fixture
async def repo(tmp_path):
    db = Database()
    await db.open(tmp_path / "plans.db")
    yield Repo(db)
    await db.close()


# ---------- PlansRepo ----------


async def test_save_overwrites_single_plan(repo: Repo):
    """全局唯一：两次 save 只有最新全文生效（UPSERT 单行）。"""
    assert await repo.plans.get_plan() is None
    await repo.plans.save_plan("r1", "旧计划")
    plan = await repo.plans.save_plan("r2", _PLAN_MD)
    loaded = await repo.plans.get_plan()
    assert loaded is not None
    assert loaded.content == _PLAN_MD and loaded.round_id == "r2"
    assert loaded.updated_at == plan.updated_at


async def test_clear_plan(repo: Repo):
    await repo.plans.save_plan("r1", _PLAN_MD)
    await repo.plans.clear_plan("r2")
    assert await repo.plans.get_plan() is None  # 空串 = 无计划


async def test_repo_rejects_overlong_content(repo: Repo):
    """长度不变量与数据同层：绕过工具层直写 repo 同样被拒。"""
    with pytest.raises(ValueError, match="超长"):
        await repo.plans.save_plan("r1", "x" * 4001)


# ---------- 工具层（registry.execute，不经风控） ----------


def _tool_deps(repo: Repo, notify_event=None) -> ToolDeps:
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
        indicator_service=None,
        daily_stats_fn=None,
        notify_event=notify_event,
        round_id="r-tool",
    )


async def test_tool_update_and_clear(repo: Repo):
    registry = ToolRegistry(_tool_deps(repo))
    out = await registry.execute("update_trade_plan", {"content": _PLAN_MD})
    assert "交易计划已更新" in out.text
    assert out.risk_verdict == ""  # 纯记录工具不经风控
    plan = await repo.plans.get_plan()
    assert plan is not None and plan.content == _PLAN_MD and plan.round_id == "r-tool"

    out = await registry.execute("clear_trade_plan", {"reason": "已按计划入场"})
    assert "交易计划已清空" in out.text
    assert await repo.plans.get_plan() is None


async def test_tool_validation_errors(repo: Repo):
    registry = ToolRegistry(_tool_deps(repo))
    out = await registry.execute("update_trade_plan", {})
    assert "参数错误" in out.text  # 缺 content
    out = await registry.execute("update_trade_plan", {"content": "x" * 4001})
    assert "参数错误" in out.text and "过长" in out.text
    out = await registry.execute("update_trade_plan", {"content": "x" * 4000})
    assert "交易计划已更新" in out.text  # 恰好到上限可过（off-by-one 护栏）
    out = await registry.execute("clear_trade_plan", {})
    assert "参数错误" in out.text  # 缺 reason（原因必须入审计）


async def test_tool_content_stripped(repo: Repo):
    """_need_str 的 strip 行为固化：落库的是去首尾空白后的全文。"""
    registry = ToolRegistry(_tool_deps(repo))
    await registry.execute("update_trade_plan", {"content": "  计划正文  "})
    plan = await repo.plans.get_plan()
    assert plan is not None and plan.content == "计划正文"


async def test_tool_clear_when_empty(repo: Repo):
    registry = ToolRegistry(_tool_deps(repo))
    out = await registry.execute("clear_trade_plan", {"reason": "x"})
    assert "本就没有交易计划" in out.text


async def test_tool_emits_plan_updated_event(repo: Repo):
    """计划变更即推 plan_updated（前端据此立即重拉）；无效变更（空清空）不推。"""
    events: list[dict] = []
    registry = ToolRegistry(_tool_deps(repo, notify_event=events.append))
    await registry.execute("update_trade_plan", {"content": _PLAN_MD})
    assert events == [{"type": "plan_updated"}]
    await registry.execute("clear_trade_plan", {"reason": "已入场"})
    assert events == [{"type": "plan_updated"}, {"type": "plan_updated"}]
    # 本就无计划时清空：无变更、不推事件
    await registry.execute("clear_trade_plan", {"reason": "x"})
    assert len(events) == 2
    # 参数校验失败：未落库也不推事件
    await registry.execute("update_trade_plan", {"content": "x" * 4001})
    assert len(events) == 2


# ---------- 上下文注入 ----------


async def _context_text(repo: Repo) -> str:
    gateway = MockGateway()
    candles = CandleCache(gateway, ManualPriceSource())
    builder = ContextBuilder(
        gateway, repo, candles, TriggerManager(lambda t, p: None), ["BTC_USDT"]
    )
    return (await builder.build("timer")).text


async def test_context_plan_empty(repo: Repo):
    text = await _context_text(repo)
    assert "## 交易计划（全局唯一一份，用 update_trade_plan 全文覆盖更新）" in text
    assert "（无）" in text


async def test_context_plan_full_text_injected(repo: Repo):
    await repo.plans.save_plan("r1", _PLAN_MD)
    text = await _context_text(repo)
    assert "## 交易计划（更新于" in text
    # 原文逐行加引用前缀定界：内容在、但不能以裸 "## " 行伪装系统 section
    assert "> ## BTC 做空" in text
    assert "> 入场：反弹 64200-64300 受阻；止损 64500；目标 63800" in text
    assert "\n## BTC 做空" not in text  # 裸标题不存在


# ---------- GET /api/plan ----------


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


async def test_api_plan_empty_and_present(client: AsyncClient, repo: Repo):
    r = await client.get("/api/plan")
    assert r.status_code == 200
    assert r.json() == {"content": "", "round_id": "", "updated_at": None}

    await repo.plans.save_plan("r9", _PLAN_MD)
    r = await client.get("/api/plan")
    body = r.json()
    assert body["content"] == _PLAN_MD and body["round_id"] == "r9"
    assert isinstance(body["updated_at"], float)
