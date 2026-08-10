"""研报端点行为测试：fake 依赖注入（tmp_path 隔离真实配置与 DB）。

覆盖：
- GET /api/research/reports(+{id})：分页/最新在前/含失败记录与逐标的摘要；
  详情逐标的 evidence/risks/narrative、404、causal_links 解析与空数组；
- GET /api/research/live：空库 round=null、进行中轮 tool_calls 组装、交易轮不串台；
- POST /api/research/run：未接线 503、LLM 未配置 503、进行中 409、成功 200 透传、
  body 透传 report_type+hours、hours 越界/非数字 422。
"""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from src.config_io import write_settings
from src.memory.db import Database
from src.memory.repo import Repo
from src.server.app import create_app
from src.server.deps import ServerDeps
from tests.research_helpers import save_report_fixture

# 长叙述：验证列表只给摘要、详情给逐标的全文
_LONG_NARRATIVE = "叙事：美联储转向预期升温，风险资产回暖。" * 20


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[Repo]:
    db = Database()
    await db.open(tmp_path / "t.db")
    yield Repo(db)
    await db.close()


def _deps(repo: Repo, tmp_path: Path, **overrides: Any) -> ServerDeps:
    """组装 fake 依赖：tmp 配置 + 指定回调覆盖。"""
    config_path = tmp_path / "config.yaml"
    write_settings({}, config_path)  # 默认配置（mode=paper）
    return ServerDeps(
        repo=repo,
        config_path=config_path,
        prompt_path=tmp_path / "system_prompt.md",
        web_dist=tmp_path / "no_dist",
        **overrides,
    )


def _client_of(deps: ServerDeps) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=create_app(deps)), base_url="http://test")


# ---------- GET /api/research/reports ----------


async def test_reports_list_pagination_and_asset_summaries(repo: Repo, tmp_path: Path):
    """列表：分页、最新在前、成功给逐标的摘要，失败给空数组与错误。"""
    r1 = await save_report_fixture(
        repo,
        report_type="asia",
        direction="偏多",
        confidence="高",
        narrative="短叙述",
    )
    r2 = await save_report_fixture(
        repo,
        report_type="manual",
        direction="偏空",
        confidence="中",
        narrative=_LONG_NARRATIVE,
        round_id="rs-round-1",
    )
    r3 = await save_report_fixture(repo, report_type="us", error="LLM 超时")
    async with _client_of(_deps(repo, tmp_path)) as c:
        body = (await c.get("/api/research/reports")).json()
        assert body["total"] == 3
        assert [i["id"] for i in body["items"]] == [r3.id, r2.id, r1.id]
        item = body["items"][1]
        assert set(item) == {
            "id",
            "report_type",
            "schema_version",
            "summary",
            "cross_market_view",
            "global_risks",
            "error",
            "round_id",
            "created_at",
            "asset_views",
        }
        assert item["round_id"] == "rs-round-1"
        assert item["asset_views"][0]["direction"] == "偏空"
        assert "narrative" not in item["asset_views"][0]
        assert body["items"][0]["error"] == "LLM 超时"
        assert body["items"][0]["asset_views"] == []
        page = (await c.get("/api/research/reports", params={"offset": 2, "limit": 2})).json()
        assert page["total"] == 3
        assert [i["id"] for i in page["items"]] == [r1.id]
        far = (await c.get("/api/research/reports", params={"offset": 9})).json()
        assert far["items"] == [] and far["total"] == 3


# ---------- GET /api/research/reports/{id} ----------


async def test_report_detail_asset_fields_and_no_raw_snapshot(repo: Repo, tmp_path: Path):
    """详情：逐标的证据、风险和研判展开；报告原文与市场快照不外泄。"""
    report = await save_report_fixture(
        repo,
        report_type="manual",
        direction="偏多",
        confidence="高",
        narrative=_LONG_NARRATIVE,
        evidence_json='[{"timeline_id": 1, "note": "ETF 净流入"}]',
        risks_json='["宏观数据反复"]',
        raw_json='{"llm": "原文"}',
        market_context={"secret": "市场快照"},
    )
    bad = await save_report_fixture(
        repo,
        report_type="asia",
        evidence_json="非JSON原文",
        risks_json="[断裂",
    )
    async with _client_of(_deps(repo, tmp_path)) as c:
        detail = (await c.get(f"/api/research/reports/{report.id}")).json()
        asset = detail["asset_views"][0]
        assert asset["narrative"] == _LONG_NARRATIVE
        assert asset["evidence"] == [{"timeline_id": 1, "note": "ETF 净流入"}]
        assert asset["risks"] == ["宏观数据反复"]
        assert "raw" not in detail
        assert "market_context_json" not in asset
        assert "市场快照" not in str(detail)
        assert detail["causal_links"] == []
        bad_asset = (await c.get(f"/api/research/reports/{bad.id}")).json()["asset_views"][0]
        assert bad_asset["evidence"] == "非JSON原文"
        assert bad_asset["risks"] == "[断裂"
        assert (await c.get("/api/research/reports/999")).status_code == 404


async def test_report_detail_causal_links(repo: Repo, tmp_path: Path):
    """详情因果链：chain/evidence 解析为对象、键集契约；他研报的链不串台。"""
    r1 = await save_report_fixture(repo, report_type="manual", direction="偏多", confidence="高")
    r2 = await save_report_fixture(repo, report_type="asia", direction="偏空", confidence="中")
    link = await repo.research.save_causal_link(
        report_id=r1.id,
        chain_json='[{"node": "加息放缓", "kind": "macro"}]',
        confidence=0.8,
        evidence_json='[{"timeline_id": 3}]',
        topic="利率",
        await_verification=False,
    )
    await repo.research.save_causal_link(report_id=r2.id, chain_json="[]", confidence=0.5)
    async with _client_of(_deps(repo, tmp_path)) as c:
        detail = (await c.get(f"/api/research/reports/{r1.id}")).json()
        links = detail["causal_links"]
        assert len(links) == 1  # r2 的链不串台
        assert set(links[0]) == {
            "id",
            "report_id",
            "chain",
            "confidence",
            "evidence",
            "status",
            "broken_at",
            "topic",
            "supersedes_id",
            "await_verification",
            "created_at",
        }
        assert links[0]["id"] == link.id
        assert links[0]["report_id"] == r1.id
        assert links[0]["chain"] == [{"node": "加息放缓", "kind": "macro"}]
        assert links[0]["confidence"] == 0.8
        assert links[0]["topic"] == "利率"
        assert links[0]["supersedes_id"] is None
        assert links[0]["await_verification"] is False
        assert links[0]["evidence"] == [{"timeline_id": 3}]
        assert links[0]["status"] == "pending"
        assert links[0]["broken_at"] is None


# ---------- GET /api/research/live ----------


async def test_research_live_empty(repo: Repo, tmp_path: Path):
    """空库：无研报轮时 round 为 null、tool_calls 为空。"""
    async with _client_of(_deps(repo, tmp_path)) as c:
        body = (await c.get("/api/research/live")).json()
        assert body == {"round": None, "tool_calls": []}


async def test_research_live_returns_in_progress_round(repo: Repo, tmp_path: Path):
    """种研报轮 + 工具调用：round 键集不含 mode、args/result 已解析；交易轮不串台。"""
    await repo.start_audit_round("rs1", "paper", wake_source="research", started_at=1000.0)
    await repo.save_audit_tool_call(
        "rs1", 1, "get_fact_timeline", '{"hours": 24}', result_json='{"events": []}'
    )
    await repo.save_audit_tool_call("rs1", 2, "submit_report", "{}", duration_ms=34)
    async with _client_of(_deps(repo, tmp_path)) as c:
        body = (await c.get("/api/research/live")).json()
        assert set(body) == {"round", "tool_calls"}
        assert set(body["round"]) == {  # 与 /api/review/live 的 round 键集一致（不含 mode）
            "round_id",
            "wake_source",
            "prompt_md5",
            "strategy_md5",
            "prompt_snapshot",
            "context_snapshot",
            "llm_raw",
            "started_at",
            "ended_at",
            "error",
        }
        assert body["round"]["round_id"] == "rs1"
        assert body["round"]["wake_source"] == "research"
        assert body["round"]["ended_at"] is None  # 进行中的研报轮同样返回
        calls = body["tool_calls"]
        assert [c["seq"] for c in calls] == [1, 2]
        assert calls[0]["tool"] == "get_fact_timeline"
        assert calls[0]["args"] == {"hours": 24}  # args_json 已解析为对象
        assert calls[0]["result"] == {"events": []}  # result_json 已解析为对象
        assert calls[1]["duration_ms"] == 34
        # 再种一条更新的交易轮：仍返回研报轮（不串台）
        await repo.start_audit_round("t-9", "paper", wake_source="timer", started_at=9000.0)
        body2 = (await c.get("/api/research/live")).json()
        assert body2["round"]["round_id"] == "rs1"


# ---------- POST /api/research/run ----------


async def test_research_run_status_mapping(repo: Repo, tmp_path: Path):
    """状态码映射走结构化 error_code（busy→409、llm_not_configured→503），不依赖错误文案。"""
    async with _client_of(_deps(repo, tmp_path)) as c:  # 未接线
        assert (await c.post("/api/research/run")).status_code == 503

    async def _busy() -> dict:
        return {"started": False, "error": "研报生成中", "error_code": "busy"}

    async def _no_llm() -> dict:
        return {
            "started": False,
            "ok": False,
            "error": "no llm",
            "error_code": "llm_not_configured",
        }

    async def _ok() -> dict:
        return {"started": True, "ok": True, "report_id": 7, "round_id": "rs-7"}

    async with _client_of(_deps(repo, tmp_path, research_run=_busy)) as c:
        assert (await c.post("/api/research/run")).status_code == 409
    async with _client_of(_deps(repo, tmp_path, research_run=_no_llm)) as c:
        assert (await c.post("/api/research/run")).status_code == 503
    async with _client_of(_deps(repo, tmp_path, research_run=_ok)) as c:
        r = await c.post("/api/research/run")
        assert r.status_code == 200
        assert r.json() == {  # 成功 200 原样透传回调结果
            "started": True,
            "ok": True,
            "report_id": 7,
            "round_id": "rs-7",
        }


async def test_research_run_body_passthrough_and_validation(repo: Repo, tmp_path: Path):
    """body 透传 report_type+hours；无 body 走无参回调（调度默认值）；hours 非法 422。"""
    calls: list[dict] = []

    async def _run(**kwargs) -> dict:
        calls.append(kwargs)
        return {"started": True, "ok": True, "report_id": len(calls)}

    async with _client_of(_deps(repo, tmp_path, research_run=_run)) as c:
        r = await c.post("/api/research/run", json={"report_type": "event", "hours": 6})
        assert r.status_code == 200 and r.json()["report_id"] == 1
        assert calls == [{"report_type": "event", "hours": 6}]  # 透传
        r = await c.post("/api/research/run")  # 无 body：调度默认值（无参回调）
        assert r.status_code == 200 and calls[-1] == {}
        assert (await c.post("/api/research/run", json={"hours": 0})).status_code == 422
        assert (await c.post("/api/research/run", json={"hours": 49})).status_code == 422
        bad = await c.post("/api/research/run", json={"hours": "abc"})
        assert bad.status_code == 422  # 非数字（FastAPI 层校验）
