"""研报端点行为测试：fake 依赖注入（tmp_path 隔离真实配置与 DB）。

覆盖：
- GET /api/research/reports(+{id})：分页/最新在前/含失败记录与逐标的摘要；
  详情逐标的 evidence/risks/narrative、404、causal_links 解析与空数组；
- GET /api/research/live：空库 round=null、进行中轮 tool_calls 组装、交易轮不串台；
- POST /api/research/run：未接线 503、LLM 未配置 503、进行中 409、成功 200 点火契约
  （含 started=true，不含执行结果字段）、body 透传 report_type+hours、hours 越界/非数字 422。
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
    """提供指向临时数据库的 Repo 实例，用毕自动关闭连接。

    参数：
        tmp_path: Path，pytest 临时目录夹具，数据库文件落在其中

    返回：
        AsyncIterator[Repo]：已打开临时数据库的仓储对象
    """
    db = Database()
    await db.open(tmp_path / "t.db")
    yield Repo(db)
    await db.close()


def _deps(repo: Repo, tmp_path: Path, **overrides: Any) -> ServerDeps:
    """组装 fake 依赖：tmp 配置 + 指定回调覆盖。

    参数：
        repo: Repo，连接测试数据库的仓储实例
        tmp_path: Path，pytest 提供的临时目录
        **overrides: Any，覆盖默认依赖的键值

    返回：
        ServerDeps，使用临时配置路径并合并指定回调覆盖的服务端依赖
    """
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
    """构造挂在 fake 应用上的异步 HTTP 测试客户端。

    参数：
        deps: ServerDeps，由 _deps 组装的服务端依赖（fake 仓储与配置）

    返回：
        AsyncClient：以 ASGI 传输直连 create_app 应用的 httpx 客户端
    """
    return AsyncClient(transport=ASGITransport(app=create_app(deps)), base_url="http://test")


# ---------- GET /api/research/schedule-status ----------


async def test_schedule_status_passthrough_and_unwired_503(repo: Repo, tmp_path: Path):
    """调度状态接线时原样返回，未接线时诚实返回 503。

    参数：
        repo: Repo，连接测试数据库的仓储实例
        tmp_path: Path，pytest 临时目录

    返回：
        None：断言状态端点的成功与未接线行为
    """
    expected = {
        "enabled": True,
        "items": [{"id": "asia_open", "enabled": True, "next_run_at": 123.0}],
        "calendar": {"state": "ok", "last_refreshed_at": 1.0, "errors": {}, "warning": ""},
    }
    deps = _deps(repo, tmp_path, research_schedule_status=lambda: expected)
    async with _client_of(deps) as client:
        assert (await client.get("/api/research/schedule-status")).json() == expected

    async with _client_of(_deps(repo, tmp_path)) as client:
        assert (await client.get("/api/research/schedule-status")).status_code == 503


# ---------- GET /api/research/reports ----------


async def test_reports_list_pagination_and_asset_summaries(repo: Repo, tmp_path: Path):
    """列表：分页、最新在前、成功给逐标的摘要，失败给空数组与错误。

    参数：
        repo: Repo，连接测试数据库的仓储实例
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证上述行为，无返回值
    """
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
        research_prompt_md5="0123456789abcdef0123456789abcdef",
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
            "research_prompt_md5",
            "created_at",
            "llm_credential_name",
            "llm_provider",
            "llm_model",
            "llm_thinking_effort",
            "asset_views",
        }
        assert item["round_id"] == "rs-round-1"
        assert item["research_prompt_md5"] == "0123456789abcdef0123456789abcdef"
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
    """详情：逐标的证据、风险和研判展开；报告原文与市场快照不外泄。

    参数：
        repo: Repo，连接测试数据库的仓储实例
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证上述行为，无返回值
    """
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
    """详情因果链：chain/evidence 解析为对象、键集契约；他研报的链不串台。

    参数：
        repo: Repo，连接测试数据库的仓储实例
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证上述行为，无返回值
    """
    r1 = await save_report_fixture(repo, report_type="manual", direction="偏多", confidence="高")
    r2 = await save_report_fixture(repo, report_type="asia", direction="偏空", confidence="中")
    link = await repo.research.save_causal_link(
        report_id=r1.id,
        chain_json='[{"node": "加息放缓", "kind": "macro"}]',
        confidence=0.8,
        evidence_json='[{"timeline_id": 3}]',
        topic="利率",
        status="concluded",
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
            "topic",
            "supersedes_id",
            "created_at",
        }
        assert links[0]["id"] == link.id
        assert links[0]["report_id"] == r1.id
        assert links[0]["chain"] == [{"node": "加息放缓", "kind": "macro"}]
        assert links[0]["confidence"] == 0.8
        assert links[0]["topic"] == "利率"
        assert links[0]["supersedes_id"] is None
        assert links[0]["evidence"] == [{"timeline_id": 3}]
        assert links[0]["status"] == "concluded"


def _asset_view_row(contract: str) -> dict[str, Any]:
    """构造最小逐标的结论字典（多标的研报夹具用，键集同 save_report_fixture 内部行）。

    参数：
        contract: str，合约名

    返回：
        dict[str, Any]：可直接传给 save_report_bundle 的逐标的结论行
    """
    return {
        "contract": contract,
        "direction": "中性",
        "confidence": "低",
        "horizon": "当日",
        "market_regime": "震荡",
        "technical_confirmation": "中性",
        "basis_type": "混合",
        "data_status": "完整",
        "evidence_json": "[]",
        "risks_json": "[]",
        "narrative": "",
        "market_context_json": "{}",
    }


async def test_report_detail_research_reviews(repo: Repo, tmp_path: Path):
    """详情研报复盘：复盘记录按 contract 分组挂到对应逐标的详情键（issue #113 C8）。

    同一研报可被多次复盘，故同一标的下是多条记录（按 id 正序）；
    未被复盘的标的 research_reviews 为空数组；evidence_reviews/outcome 解析为对象。

    参数：
        repo: Repo，连接测试数据库的仓储实例
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证分组挂载、键集契约与 JSON 解析，无返回值
    """
    report, _views = await repo.research.save_report_bundle(
        report_type="manual",
        summary="",
        cross_market_view="",
        global_risks_json="[]",
        raw_json="{}",
        round_id="",
        asset_views=[_asset_view_row("BTC_USDT"), _asset_view_row("ETH_USDT")],
    )
    first = await repo.research_review.save_review(
        review_report_id=1,
        report_id=report.id,
        contract="BTC_USDT",
        direction_relation="realized",
        direction_reason="方向正确",
        reasoning_quality="sound",
        reasoning_review="链条完整",
        evidence_reviews_json=(
            '[{"evidence_index": 1, "fact_status": "confirmed",'
            ' "reasoning_status": "supported", "explanation": "依据成立"}]'
        ),
        confidence_assessment="appropriate",
        confidence_reason="置信度合规",
        improvement_advice="继续保持",
        outcome_json='{"data_status": "ok", "return_pct": 1.2}',
    )
    second = await repo.research_review.save_review(
        review_report_id=2,
        report_id=report.id,
        contract="BTC_USDT",
        direction_relation="diverged",
    )
    async with _client_of(_deps(repo, tmp_path)) as c:
        detail = (await c.get(f"/api/research/reports/{report.id}")).json()
        by_contract = {v["contract"]: v for v in detail["asset_views"]}
        btc_reviews = by_contract["BTC_USDT"]["research_reviews"]
        assert [r["id"] for r in btc_reviews] == [first.id, second.id]  # 多条按 id 正序
        assert set(btc_reviews[0]) == {
            "id",
            "review_report_id",
            "direction_relation",
            "direction_reason",
            "reasoning_quality",
            "reasoning_review",
            "evidence_reviews",
            "confidence_assessment",
            "confidence_reason",
            "improvement_advice",
            "outcome",
            "created_at",
        }
        assert btc_reviews[0]["review_report_id"] == 1
        assert btc_reviews[0]["evidence_reviews"] == [
            {
                "evidence_index": 1,
                "fact_status": "confirmed",
                "reasoning_status": "supported",
                "explanation": "依据成立",
            }
        ]
        assert btc_reviews[0]["outcome"] == {"data_status": "ok", "return_pct": 1.2}
        assert btc_reviews[1]["outcome"] == {}  # 默认 outcome_json '{}' 解析为空对象
        assert by_contract["ETH_USDT"]["research_reviews"] == []  # 未复盘标的给空数组


# ---------- GET /api/research/live ----------


async def test_research_live_empty(repo: Repo, tmp_path: Path):
    """空库：无研报轮时 round 为 null、tool_calls 为空。

    参数：
        repo: Repo，连接测试数据库的仓储实例
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证上述行为，无返回值
    """
    async with _client_of(_deps(repo, tmp_path)) as c:
        body = (await c.get("/api/research/live")).json()
        assert body == {"round": None, "tool_calls": []}


async def test_research_live_returns_in_progress_round(repo: Repo, tmp_path: Path):
    """种研报轮 + 工具调用：round 键集不含 mode、args/result 已解析；交易轮不串台。

    参数：
        repo: Repo，连接测试数据库的仓储实例
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证上述行为，无返回值
    """
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
            "llm_credential_name",
            "llm_provider",
            "llm_model",
            "llm_thinking_effort",
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


async def test_research_live_by_round_id(repo: Repo, tmp_path: Path):
    """?round_id= 直查：指定轮优先于最新轮；查无或异类轮（wake_source 不符）按空态返回。

    参数：
        repo: Repo，连接测试数据库的仓储实例
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，断言指定轮命中带 tool_calls、查无此轮与异类轮均返回空态
    """
    await repo.start_audit_round("rs1", "paper", wake_source="research", started_at=1000.0)
    await repo.save_audit_tool_call(
        "rs1", 1, "get_fact_timeline", '{"hours": 24}', result_json='{"events": []}'
    )
    await repo.start_audit_round("rs2", "paper", wake_source="research", started_at=2000.0)
    await repo.start_audit_round("rv1", "paper", wake_source="review", started_at=3000.0)
    async with _client_of(_deps(repo, tmp_path)) as c:
        # 指定已存在轮：即使存在更新的研报轮（rs2）也返回指定轮及其 tool_calls
        body = (await c.get("/api/research/live", params={"round_id": "rs1"})).json()
        assert body["round"]["round_id"] == "rs1"
        assert body["round"]["wake_source"] == "research"
        assert [tc["seq"] for tc in body["tool_calls"]] == [1]
        # 查无此轮：空态（HTTP 仍 200，供前端 pinned 轮询）
        missing = (await c.get("/api/research/live", params={"round_id": "no-such"})).json()
        assert missing == {"round": None, "tool_calls": []}
        # 异类 wake_source（复盘轮）：同样按空态返回，不跨台
        other = (await c.get("/api/research/live", params={"round_id": "rv1"})).json()
        assert other == {"round": None, "tool_calls": []}


# ---------- POST /api/research/run ----------


async def test_research_run_status_mapping(repo: Repo, tmp_path: Path):
    """状态码映射走结构化 error_code（busy→409、llm_not_configured→503），不依赖错误文案。

    参数：
        repo: Repo，连接测试数据库的仓储实例
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证上述行为，无返回值
    """
    async with _client_of(_deps(repo, tmp_path)) as c:  # 未接线
        assert (await c.post("/api/research/run")).status_code == 503

    async def _busy() -> dict:
        """假研报回调：返回 busy 错误码，触发 409。

        参数：无

        返回：
            dict：未启动、error_code 为 busy 的假研报运行结果
        """
        return {"started": False, "error": "研报生成中", "error_code": "busy"}

    async def _no_llm() -> dict:
        """假研报回调：返回 llm_not_configured 错误码，触发 503。

        参数：无

        返回：
            dict：未启动、error_code 为 llm_not_configured 的假研报运行结果
        """
        return {
            "started": False,
            "ok": False,
            "error": "no llm",
            "error_code": "llm_not_configured",
        }

    async def _ok() -> dict:
        """假研报回调：返回点火结果，验证 200 点火契约（不含执行结果字段）。

        参数：无

        返回：
            dict：点火成功的假结果（started + 预分配 round_id + 回显参数）
        """
        return {"started": True, "report_type": "manual", "hours": 24, "round_id": "ab" * 16}

    async with _client_of(_deps(repo, tmp_path, research_run=_busy)) as c:
        assert (await c.post("/api/research/run")).status_code == 409
    async with _client_of(_deps(repo, tmp_path, research_run=_no_llm)) as c:
        assert (await c.post("/api/research/run")).status_code == 503
    async with _client_of(_deps(repo, tmp_path, research_run=_ok)) as c:
        r = await c.post("/api/research/run")
        assert r.status_code == 200
        assert r.json() == {  # 点火即返回：started + 预分配 round_id + 回显参数，不含执行结果
            "started": True,
            "report_type": "manual",
            "hours": 24,
            "round_id": "ab" * 16,
        }


async def test_research_run_body_passthrough_and_validation(repo: Repo, tmp_path: Path):
    """body 透传 report_type+hours；无 body 走无参回调（调度默认值）；hours 非法 422。

    参数：
        repo: Repo，连接测试数据库的仓储实例
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证上述行为，无返回值
    """
    calls: list[dict] = []

    async def _run(**kwargs) -> dict:
        """假研报回调：记录收到的 kwargs，返回点火结果。

        参数：
            kwargs: dict，POST body 透传给研报回调的参数（如 report_type、hours）

        返回：
            dict：点火成功的假结果（started + 回显参数，缺省走调度默认值）
        """
        calls.append(kwargs)
        return {
            "started": True,
            "report_type": kwargs.get("report_type", "manual"),
            "hours": kwargs.get("hours", 24),
            "round_id": "cd" * 16,  # 预分配轮次编号（契约键，原样透传）
        }

    async with _client_of(_deps(repo, tmp_path, research_run=_run)) as c:
        r = await c.post("/api/research/run", json={"report_type": "event", "hours": 6})
        assert r.status_code == 200
        assert r.json() == {  # 点火回显：started + 预分配 round_id + 透传参数
            "started": True,
            "report_type": "event",
            "hours": 6,
            "round_id": "cd" * 16,
        }
        assert calls == [{"report_type": "event", "hours": 6}]  # 透传
        r = await c.post("/api/research/run")  # 无 body：调度默认值（无参回调）
        assert r.status_code == 200 and calls[-1] == {}
        assert (await c.post("/api/research/run", json={"hours": 0})).status_code == 422
        assert (await c.post("/api/research/run", json={"hours": 49})).status_code == 422
        bad = await c.post("/api/research/run", json={"hours": "abc"})
        assert bad.status_code == 422  # 非数字（FastAPI 层校验）
