"""模型身份（LLMIdentity）全链路测试：provider 携带 → 开轮落库 → 迁移幂等 → API 透出。

覆盖：identity_of 鸭子类型回退、三个真实 provider 与 RetryingProvider 的身份、
begin_round 落库、旧库补列迁移幂等、/api/rounds 摘要与复盘/研报报告接口透出四键。
"""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from httpx import ASGITransport, AsyncClient

from src.agent.providers.anthropic import AnthropicProvider
from src.agent.providers.mock import MockProvider
from src.agent.providers.openai_compat import OpenAICompatProvider
from src.agent.providers.openai_responses import OpenAIResponsesProvider
from src.agent.providers.retry import RetryingProvider
from src.audit.trail import AuditTrail
from src.config import AuditConfig, CredentialConfig, LLMConfig
from src.config_io import write_settings
from src.memory.db import Database
from src.memory.repo import Repo
from src.server.app import create_app
from src.server.deps import ServerDeps
from src.utils import LLMIdentity, identity_of
from tests.research_helpers import save_report_fixture

_IDENTITY = LLMIdentity(
    credential_name="ds-main",
    provider="openai_compat",
    model="deepseek-v4-flash",
    thinking_effort="high",
)


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[Repo]:
    """构造指向临时数据库的 Repo 夹具，测试结束后关闭连接。

    参数：
        tmp_path: Path，pytest 临时目录夹具

    返回：
        AsyncIterator[Repo]：yield 已打开临时数据库的仓储对象
    """
    db = Database()
    await db.open(tmp_path / "t.db")
    yield Repo(db)
    await db.close()


@pytest.fixture
def trail(repo: Repo, tmp_path: Path) -> AuditTrail:
    """构造使用临时数据库与临时快照目录的 AuditTrail 夹具。

    参数：
        repo: Repo，临时数据库仓储夹具
        tmp_path: Path，pytest 临时目录夹具

    返回：
        AuditTrail：绑定临时仓储与快照目录的审计追踪对象
    """
    return AuditTrail(repo, AuditConfig(dir=str(tmp_path / "audit")))


# ---------- identity_of 与 provider 携带身份 ----------


def test_identity_of_fallback_and_retry_forwarding():
    """identity_of 对无身份对象回退全空；MockProvider 可注入身份；重试包装层透传。

    参数：无
    返回：None，执行断言验证目标行为
    """
    assert identity_of(object()) == LLMIdentity()  # 测试桩无 identity 属性 → 全空
    assert identity_of(MockProvider()) == LLMIdentity()  # 默认全空
    wrapped = RetryingProvider(MockProvider(identity=_IDENTITY), max_attempts=1)
    assert identity_of(wrapped) == _IDENTITY  # RetryingProvider 透传内层身份


def test_real_providers_carry_credential_identity():
    """三个真实 provider：凭证配置带凭证名，旧平铺 LLMConfig 凭证名为空。

    参数：无
    返回：None，执行断言验证目标行为
    """
    for cls, provider_name in (
        (OpenAICompatProvider, "openai_compat"),
        (OpenAIResponsesProvider, "openai_responses"),
        (AnthropicProvider, "anthropic"),
    ):
        cred = CredentialConfig(
            name="ds-main",
            provider=provider_name,  # type: ignore[arg-type]
            model="deepseek-v4-flash",
            thinking_effort="high",
        )
        provider = cls(cred, api_key="k")  # 仅建 SDK 客户端，不触网
        assert identity_of(provider) == LLMIdentity(
            credential_name="ds-main",
            provider=provider_name,
            model="deepseek-v4-flash",
            thinking_effort="high",
        )
    legacy = LLMConfig(provider="openai_compat", model="m1", thinking_effort="low")
    assert identity_of(OpenAICompatProvider(legacy, api_key="k")) == LLMIdentity(
        credential_name="", provider="openai_compat", model="m1", thinking_effort="low"
    )


# ---------- 开轮落库 ----------


async def test_begin_round_persists_identity(repo: Repo, trail: AuditTrail):
    """begin_round 带身份落库四字段；不带身份按全空落库（历史/未知口径）。

    参数：
        repo: Repo，临时数据库仓储夹具
        trail: AuditTrail，审计追踪夹具
    返回：None，执行断言验证目标行为
    """
    round_id = await trail.begin_round("paper", "timer", "提示词", llm_identity=_IDENTITY)
    row = await repo.get_audit_round(round_id)
    assert row is not None
    assert row.llm_credential_name == "ds-main"
    assert row.llm_provider == "openai_compat"
    assert row.llm_model == "deepseek-v4-flash"
    assert row.llm_thinking_effort == "high"

    plain = await trail.begin_round("paper", "timer", "提示词")
    row2 = await repo.get_audit_round(plain)
    assert row2 is not None
    assert (row2.llm_credential_name, row2.llm_model) == ("", "")


# ---------- 旧库迁移幂等 ----------

# 旧版 audit_rounds 表结构（无模型身份四列），与功能上线前一致
_OLD_AUDIT_ROUNDS_DDL = """
CREATE TABLE audit_rounds (
    round_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    wake_source TEXT NOT NULL DEFAULT '',
    prompt_md5 TEXT NOT NULL DEFAULT '',
    strategy_md5 TEXT NOT NULL DEFAULT '',
    prompt_snapshot TEXT NOT NULL DEFAULT '',
    context_snapshot TEXT NOT NULL DEFAULT '',
    llm_raw TEXT NOT NULL DEFAULT '',
    started_at REAL NOT NULL,
    ended_at REAL,
    error TEXT NOT NULL DEFAULT ''
)
"""


async def test_audit_rounds_identity_migration(tmp_path: Path):
    """旧库（audit_rounds 无身份四列）迁移补列；老行保持 ''，新行正常写入，重复 open 幂等。

    参数：
        tmp_path: Path，pytest 临时目录夹具
    返回：None，执行断言验证目标行为
    """
    path = tmp_path / "old.db"
    conn = await aiosqlite.connect(str(path))
    await conn.execute(_OLD_AUDIT_ROUNDS_DDL)
    await conn.execute(
        "INSERT INTO audit_rounds(round_id,mode,started_at) VALUES('old-1','paper',1000.0)"
    )
    await conn.commit()
    await conn.close()

    db = Database()
    await db.open(path)  # open 执行完整 SCHEMA（IF NOT EXISTS 不动旧表）+ _migrate 补列
    cur = await db.conn.execute("PRAGMA table_info(audit_rounds)")
    cols = {row["name"] for row in await cur.fetchall()}
    assert {"llm_credential_name", "llm_provider", "llm_model", "llm_thinking_effort"} <= cols
    repo = Repo(db)
    old = await repo.get_audit_round("old-1")
    assert old is not None and old.llm_model == ""  # 老行无法推断模型，保持 '' 不回填
    await repo.start_audit_round("new-1", "paper", llm_identity=_IDENTITY)
    new = await repo.get_audit_round("new-1")
    assert new is not None and new.llm_model == "deepseek-v4-flash"
    await db.close()

    db2 = Database()
    await db2.open(path)  # 重复 open 幂等（列已存在，不再 ALTER）
    await db2.close()


# ---------- API 透出 ----------


def _deps(repo: Repo, tmp_path: Path, **overrides: Any) -> ServerDeps:
    """组装使用临时配置的服务器依赖（与 test_server_review 同款最小装配）。

    参数：
        repo: Repo，端点读写使用的仓储
        tmp_path: Path，pytest 临时目录
        overrides: Any，按名称覆盖默认 ServerDeps 字段

    返回：
        ServerDeps，可注入测试应用的依赖集合
    """
    config_path = tmp_path / "config.yaml"
    write_settings({}, config_path)
    prompt_path = tmp_path / "system_prompt.md"
    prompt_path.write_text("原始提示词", encoding="utf-8")
    return ServerDeps(
        repo=repo,
        config_path=config_path,
        prompt_path=prompt_path,
        web_dist=tmp_path / "no_dist",
        **overrides,
    )


async def _seed_round(repo: Repo, round_id: str, wake_source: str) -> None:
    """种子一轮带模型身份的审计轮。

    参数：
        repo: Repo，临时数据库仓储
        round_id: str，轮次编号
        wake_source: str，唤醒来源（timer/review/research）

    返回：None，写入数据库
    """
    await repo.start_audit_round(
        round_id, "paper", wake_source=wake_source, started_at=1000.0, llm_identity=_IDENTITY
    )


async def test_rounds_api_audit_summary_includes_identity(
    repo: Repo, trail: AuditTrail, tmp_path: Path
):
    """/api/rounds 列表的 audit 摘要与 /rounds/{id} 详情均带模型身份四键。

    参数：
        repo: Repo，临时数据库仓储夹具
        trail: AuditTrail，审计追踪夹具（详情路由依赖注入用）
        tmp_path: Path，pytest 临时目录夹具
    返回：None，执行断言验证目标行为
    """
    await _seed_round(repo, "r-id", "timer")
    await repo.save_decision(round_id="r-id", mode="paper")
    transport = ASGITransport(app=create_app(_deps(repo, tmp_path, audit_trail=trail)))
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        body = (await c.get("/api/rounds")).json()
        audit = body["items"][0]["audit"]
        assert audit["llm_credential_name"] == "ds-main"
        assert audit["llm_provider"] == "openai_compat"
        assert audit["llm_model"] == "deepseek-v4-flash"
        assert audit["llm_thinking_effort"] == "high"
        detail = (await c.get("/api/rounds/r-id")).json()
        assert detail["llm_model"] == "deepseek-v4-flash"
        assert detail["llm_thinking_effort"] == "high"


async def test_review_reports_api_includes_identity(repo: Repo, tmp_path: Path):
    """/api/review/reports 列表与详情按 round_id 关联带出模型身份；无关联报告四键为空。

    参数：
        repo: Repo，临时数据库仓储夹具
        tmp_path: Path，pytest 临时目录夹具
    返回：None，执行断言验证目标行为
    """
    await _seed_round(repo, "rv-id", "review")
    await repo.review.save_review_report(1000.0, 2000.0, "{}", "# 报告", "none", round_id="rv-id")
    orphan = await repo.review.save_review_report(1000.0, 2000.0, "{}", "# 老报告", "none")
    transport = ASGITransport(app=create_app(_deps(repo, tmp_path)))
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        items = (await c.get("/api/review/reports")).json()["items"]
        linked = next(i for i in items if i["round_id"] == "rv-id")
        assert linked["llm_model"] == "deepseek-v4-flash"
        assert linked["llm_credential_name"] == "ds-main"
        old = next(i for i in items if i["id"] == orphan.id)
        assert old["llm_model"] == ""  # 无关联审计轮 → 身份未知
        detail = (await c.get(f"/api/review/reports/{linked['id']}")).json()
        assert detail["llm_thinking_effort"] == "high"


async def test_research_reports_api_includes_identity(repo: Repo, tmp_path: Path):
    """/api/research/reports 列表与详情按 round_id 关联带出模型身份。

    参数：
        repo: Repo，临时数据库仓储夹具
        tmp_path: Path，pytest 临时目录夹具
    返回：None，执行断言验证目标行为
    """
    await _seed_round(repo, "rs-id", "research")
    report = await save_report_fixture(repo, report_type="manual", round_id="rs-id")
    transport = ASGITransport(app=create_app(_deps(repo, tmp_path)))
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        items = (await c.get("/api/research/reports")).json()["items"]
        assert items[0]["llm_model"] == "deepseek-v4-flash"
        detail = (await c.get(f"/api/research/reports/{report.id}")).json()
        assert detail["llm_provider"] == "openai_compat"
        assert detail["llm_thinking_effort"] == "high"
