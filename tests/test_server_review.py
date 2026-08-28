"""复盘/策略版本端点行为测试：fake 依赖注入（tmp_path 隔离真实配置与 DB）。

覆盖：
- GET /api/review/reports(+{id})：列表截断 200、详情全文、404；
- GET /api/review/live：空库 round=null、键集与值（args/result 已解析）、交易轮不串台；
- POST /api/review/run：未接线 503、LLM 未配置 503、进行中 409、成功 200；
- GET /api/strategy/versions(+{id})：列表不含 content、详情含 content、404；
- GET /api/strategy/diff：200 纯文本、版本不存在 404、参数非法 422；
- POST /api/strategy/rollback/{id}：成功 200、版本不存在 404、未接线 503；
- PUT /api/strategy 走 strategy_save（响应保持 PlainText 原文、422 路径、无差异幂等路径）；
- PUT /api/config 的 review.enabled/daily_time/interval_days 热写回运行时（_RUNTIME_KEYS）；
- POST /api/review/research/rereview：人工重评授权登记（404/409/422/200 与幂等复用，R5-2）。
"""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from src.config import Settings
from src.config_io import write_settings
from src.memory.db import Database
from src.memory.repo import Repo
from src.review.strategy import StrategyValidationError
from src.server.app import create_app
from src.server.deps import ServerDeps

# 长报告（>200 字符）：验证列表截断与详情全文
_LONG_MD = "# 复盘报告\n\n" + "区间成交 3 笔，胜率 66.7%。" * 30


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[Repo]:
    """构造指向临时数据库的 Repo 实例，测试结束后关闭连接。

    参数：
        tmp_path: Path，pytest 临时目录夹具，数据库文件落在其中

    返回：
        AsyncIterator[Repo]：yield 已打开临时数据库的仓储对象
    """
    db = Database()
    await db.open(tmp_path / "t.db")
    yield Repo(db)
    await db.close()


def _deps(repo: Repo, tmp_path: Path, **overrides: Any) -> ServerDeps:
    """组装使用临时配置和策略书且支持回调覆盖的服务器依赖。

    参数：
        repo: Repo，端点读写复盘数据使用的仓储
        tmp_path: Path，pytest 临时目录
        overrides: Any，按名称覆盖默认 ServerDeps 字段

    返回：
        ServerDeps，可注入测试应用的依赖集合
    """
    config_path = tmp_path / "config.yaml"
    write_settings({}, config_path)  # 默认配置（mode=paper，review.enabled=true）
    prompt_path = tmp_path / "system_prompt.md"
    prompt_path.write_text("原始提示词", encoding="utf-8")
    return ServerDeps(
        repo=repo,
        config_path=config_path,
        prompt_path=prompt_path,
        web_dist=tmp_path / "no_dist",
        **overrides,
    )


def _client_of(deps: ServerDeps) -> AsyncClient:
    """为指定依赖创建直接调用 ASGI 应用的异步客户端。

    参数：
        deps: ServerDeps，待注入应用的服务器依赖

    返回：
        AsyncClient，以 http://test 为基址的进程内测试客户端
    """
    return AsyncClient(transport=ASGITransport(app=create_app(deps)), base_url="http://test")


# ---------- GET /api/review/reports(+{id}) ----------


async def test_review_reports_list_and_detail(repo: Repo, tmp_path: Path):
    """验证复盘报告列表截断正文、保持倒序并由详情端点返回全文。

    参数：
        repo: Repo，用于预置复盘报告的仓储
        tmp_path: Path，pytest 临时目录

    返回：
        None，通过断言验证列表字段、详情全文及不存在报告的 404
    """
    await repo.review.save_review_report(1000.0, 2000.0, '{"a":1}', "短报告", "none")
    r2 = await repo.review.save_review_report(
        3000.0, 4000.0, '{"b":2}', _LONG_MD, "rewrite", new_version_id=7, round_id="rv-round-1"
    )
    async with _client_of(_deps(repo, tmp_path)) as c:
        body = (await c.get("/api/review/reports")).json()
        assert body["total"] == 2
        assert [i["id"] for i in body["items"]] == [r2.id, r2.id - 1]  # 最新在前
        item = body["items"][0]
        assert len(item["report_md"]) == 200  # 列表截断
        assert item["new_version_id"] == 7 and item["strategy_action"] == "rewrite"
        assert item["round_id"] == "rv-round-1"  # 列表项透出审计轮 id
        assert body["items"][1]["round_id"] == ""  # 省略参数默认 ''（无关联）
        detail = (await c.get(f"/api/review/reports/{r2.id}")).json()
        assert detail["report_md"] == _LONG_MD  # 详情全文
        assert detail["round_id"] == "rv-round-1"  # 详情透出审计轮 id
        assert (await c.get("/api/review/reports/999")).status_code == 404


# ---------- GET /api/review/live ----------


async def test_review_live_empty(repo: Repo, tmp_path: Path):
    """验证空数据库的复盘实时端点返回空轮次和空工具调用。

    参数：
        repo: Repo，空临时数据库仓储
        tmp_path: Path，pytest 临时目录

    返回：
        None，通过断言验证固定空态响应契约
    """
    async with _client_of(_deps(repo, tmp_path)) as c:
        body = (await c.get("/api/review/live")).json()
        assert body == {"round": None, "tool_calls": []}


async def test_review_live_returns_latest_review_round(repo: Repo, tmp_path: Path):
    """验证复盘实时端点返回最新复盘轮、解析工具字段且不串入交易轮。

    参数：
        repo: Repo，用于预置审计轮与工具调用的仓储
        tmp_path: Path，pytest 临时目录

    返回：
        None，通过断言验证轮次键集、工具解析、进行中状态和角色隔离
    """
    await repo.start_audit_round("rv1", "paper", wake_source="review", started_at=1000.0)
    await repo.save_audit_tool_call(
        "rv1", 1, "get_review_stats", '{"start_ts": 1000}', result_json='{"text": "概览"}'
    )
    await repo.save_audit_tool_call("rv1", 2, "list_trades", "{}", duration_ms=12)
    async with _client_of(_deps(repo, tmp_path)) as c:
        body = (await c.get("/api/review/live")).json()
        assert set(body) == {"round", "tool_calls"}
        assert set(body["round"]) == {  # 与 /api/agent/live 的 round 键集一致（不含 mode）
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
        assert body["round"]["round_id"] == "rv1"
        assert body["round"]["wake_source"] == "review"
        assert body["round"]["ended_at"] is None  # 进行中的复盘轮同样返回
        calls = body["tool_calls"]
        assert [c["seq"] for c in calls] == [1, 2]
        assert set(calls[0]) == {
            "seq",
            "tool",
            "args",
            "risk_verdict",
            "risk_reason",
            "result",
            "duration_ms",
        }
        assert calls[0]["tool"] == "get_review_stats"
        assert calls[0]["args"] == {"start_ts": 1000}  # args_json 已解析为对象
        assert calls[0]["result"] == {"text": "概览"}  # result_json 已解析为对象
        assert calls[1]["duration_ms"] == 12
        # 再种一条更新的交易轮：仍返回复盘轮（不串台）
        await repo.start_audit_round("r-t9", "paper", wake_source="timer", started_at=9000.0)
        body2 = (await c.get("/api/review/live")).json()
        assert body2["round"]["round_id"] == "rv1"


async def test_review_live_by_round_id(repo: Repo, tmp_path: Path):
    """?round_id= 直查：指定轮优先于最新轮；查无或异类轮（wake_source 不符）按空态返回。

    参数：
        repo: Repo，连接测试数据库的仓储实例
        tmp_path: Path，pytest 临时目录

    返回：
        None，断言指定轮命中带 tool_calls、查无此轮与异类轮均返回空态
    """
    await repo.start_audit_round("rv1", "paper", wake_source="review", started_at=1000.0)
    await repo.save_audit_tool_call(
        "rv1", 1, "get_review_stats", '{"start_ts": 1000}', result_json='{"text": "概览"}'
    )
    await repo.start_audit_round("rv2", "paper", wake_source="review", started_at=2000.0)
    await repo.start_audit_round("rs1", "paper", wake_source="research", started_at=3000.0)
    async with _client_of(_deps(repo, tmp_path)) as c:
        # 指定已存在轮：即使存在更新的复盘轮（rv2）也返回指定轮及其 tool_calls
        body = (await c.get("/api/review/live", params={"round_id": "rv1"})).json()
        assert body["round"]["round_id"] == "rv1"
        assert body["round"]["wake_source"] == "review"
        assert [tc["seq"] for tc in body["tool_calls"]] == [1]
        # 查无此轮：空态（HTTP 仍 200，供前端 pinned 轮询）
        missing = (await c.get("/api/review/live", params={"round_id": "no-such"})).json()
        assert missing == {"round": None, "tool_calls": []}
        # 异类 wake_source（研报轮）：同样按空态返回，不跨台
        other = (await c.get("/api/review/live", params={"round_id": "rs1"})).json()
        assert other == {"round": None, "tool_calls": []}


# ---------- POST /api/review/run ----------


async def test_review_run_status_mapping(repo: Repo, tmp_path: Path):
    """验证手动复盘按结构化错误码映射未接线、忙碌、缺模型和成功状态。

    参数：
        repo: Repo，临时数据库仓储
        tmp_path: Path，pytest 临时目录

    返回：
        None，通过断言验证 503、409 与 200 三类响应
    """
    async with _client_of(_deps(repo, tmp_path)) as c:  # 未接线
        r = await c.post("/api/review/run")
        assert r.status_code == 503

    async def _busy() -> dict:
        """模拟复盘调度器正忙的结构化结果。

        参数：无

        返回：
            dict，包含 busy 错误码的未启动结果
        """
        return {"started": False, "error": "busy right now", "error_code": "busy"}

    async def _no_llm() -> dict:
        """模拟复盘缺少模型配置的结构化结果。

        参数：无

        返回：
            dict，包含 llm_not_configured 错误码的失败结果
        """
        return {
            "started": False,
            "ok": False,
            "error": "no llm at all",
            "error_code": "llm_not_configured",
        }

    async def _ok() -> dict:
        """模拟复盘点火成功（后台执行，响应不含执行结果字段）。

        参数：无

        返回：
            dict，点火成功的假结果（started + 预分配 round_id + 回显区间）
        """
        return {
            "started": True,
            "period_start": 1000.0,
            "period_end": 2000.0,
            "round_id": "ef" * 16,
        }

    async with _client_of(_deps(repo, tmp_path, review_run=_busy)) as c:
        assert (await c.post("/api/review/run")).status_code == 409
    async with _client_of(_deps(repo, tmp_path, review_run=_no_llm)) as c:
        assert (await c.post("/api/review/run")).status_code == 503
    async with _client_of(_deps(repo, tmp_path, review_run=_ok)) as c:
        r = await c.post("/api/review/run")
        assert r.status_code == 200
        assert r.json() == {  # 点火即返回：started + 预分配 round_id + 回显区间，不含执行结果
            "started": True,
            "period_start": 1000.0,
            "period_end": 2000.0,
            "round_id": "ef" * 16,
        }


async def test_review_run_with_explicit_period(repo: Repo, tmp_path: Path):
    """验证手动复盘端点透传可选补跑区间并拒绝非法请求。

    参数：
        repo: Repo，临时数据库仓储
        tmp_path: Path，pytest 临时目录

    返回：
        None，通过断言验证有参、无参及两类非法区间请求
    """
    calls: list[dict] = []

    async def _run(**kwargs) -> dict:
        """记录复盘区间参数并返回点火结果。

        参数：
            kwargs: dict，路由透传的 period_start(开始时间)与 period_end(结束时间)

        返回：
            dict，点火成功的假结果（started + 回显区间，缺省走调度默认区间）
        """
        calls.append(kwargs)
        return {
            "started": True,
            "period_start": kwargs.get("period_start", 0.0),
            "period_end": kwargs.get("period_end", 0.0),
            "round_id": "01" * 16,  # 预分配轮次编号（契约键，原样透传）
        }

    async def _invalid(**kwargs) -> dict:
        """模拟复盘调度器拒绝非法时间区间。

        参数：
            kwargs: dict，路由透传的复盘区间，本桩不读取其内容

        返回：
            dict，包含 invalid_period 错误码的未启动结果
        """
        return {"started": False, "error": "区间非法", "error_code": "invalid_period"}

    async with _client_of(_deps(repo, tmp_path, review_run=_run)) as c:
        r = await c.post("/api/review/run", json={"start_ts": 1000.0, "end_ts": 2000.0})
        assert r.status_code == 200
        assert r.json() == {  # 点火回显区间 + 预分配 round_id，不含执行结果
            "started": True,
            "period_start": 1000.0,
            "period_end": 2000.0,
            "round_id": "01" * 16,
        }
        assert calls == [{"period_start": 1000.0, "period_end": 2000.0}]  # 区间透传
        r = await c.post("/api/review/run")  # 无 body：维持昨日区间（无参回调）
        assert r.status_code == 200 and calls[-1] == {}
        bad = await c.post("/api/review/run", json={"start_ts": "abc", "end_ts": 2000.0})
        assert bad.status_code == 422  # 非数字（FastAPI 层校验）
        missing = await c.post("/api/review/run", json={"start_ts": 1000.0})
        assert missing.status_code == 422  # 缺 end_ts
    async with _client_of(_deps(repo, tmp_path, review_run=_invalid)) as c:
        # start>=end 由 scheduler 判定 → error_code=invalid_period → 422
        r = await c.post("/api/review/run", json={"start_ts": 3000.0, "end_ts": 2000.0})
        assert r.status_code == 422


# ---------- GET /api/strategy/versions(+{id}) 与 diff ----------


async def test_strategy_versions_list_detail_and_diff(repo: Repo, tmp_path: Path):
    """验证策略版本列表、详情和差异端点的字段、文本与错误响应。

    参数：
        repo: Repo，用于预置两条策略版本的仓储
        tmp_path: Path，pytest 临时目录

    返回：
        None，通过断言验证倒序列表、详情正文、差异文本及 404、422
    """
    v1 = await repo.review.save_strategy_version(
        "策略书 v1：保守止损。", "md5-v1", "human", "初始版本"
    )
    v2 = await repo.review.save_strategy_version(
        "策略书 v2：收紧止损。", "md5-v2", "review_agent", "复盘改写"
    )
    async with _client_of(_deps(repo, tmp_path)) as c:
        items = (await c.get("/api/strategy/versions")).json()["items"]
        assert [i["id"] for i in items] == [v2.id, v1.id]  # 最新在前
        assert all("content" not in i for i in items)  # 列表不含全文
        detail = (await c.get(f"/api/strategy/versions/{v1.id}")).json()
        assert detail["content"] == "策略书 v1：保守止损。"
        assert (await c.get("/api/strategy/versions/999")).status_code == 404

        r = await c.get(f"/api/strategy/diff?from={v1.id}&to={v2.id}")
        assert r.status_code == 200
        assert "-策略书 v1：保守止损。" in r.text and "+策略书 v2：收紧止损。" in r.text
        assert (await c.get(f"/api/strategy/diff?from=999&to={v2.id}")).status_code == 404
        assert (await c.get(f"/api/strategy/diff?from=abc&to={v2.id}")).status_code == 422
        assert (await c.get(f"/api/strategy/diff?to={v2.id}")).status_code == 422  # 缺 from


# ---------- POST /api/strategy/rollback/{id} ----------


async def test_strategy_rollback_status_mapping(repo: Repo, tmp_path: Path):
    """验证策略回滚端点的成功、版本不存在和未接线状态映射。

    参数：
        repo: Repo，临时数据库仓储
        tmp_path: Path，pytest 临时目录

    返回：
        None，通过断言验证 200、404 与 503 三类响应
    """

    async def _ok(version_id: int) -> dict:
        """模拟成功回滚到指定策略版本。

        参数：
            version_id: int，请求回滚到的历史版本编号

        返回：
            dict，包含目标版本与固定新版本编号 3
        """
        return {"rolled_back_to": version_id, "version": 3}

    async def _missing(version_id: int) -> dict:
        """模拟目标策略版本不存在。

        参数：
            version_id: int，请求回滚到的版本编号

        返回：
            dict，本函数始终在返回前抛出异常

        异常：
            StrategyValidationError: 每次调用均携带版本不存在原因抛出
        """
        raise StrategyValidationError([f"策略版本 v{version_id} 不存在，无法回滚"])

    async with _client_of(_deps(repo, tmp_path, strategy_rollback=_ok)) as c:
        r = await c.post("/api/strategy/rollback/1")
        assert r.status_code == 200
        assert r.json() == {"rolled_back_to": 1, "version": 3}
    async with _client_of(_deps(repo, tmp_path, strategy_rollback=_missing)) as c:
        assert (await c.post("/api/strategy/rollback/9")).status_code == 404
    async with _client_of(_deps(repo, tmp_path)) as c:  # 未接线
        assert (await c.post("/api/strategy/rollback/1")).status_code == 503


# ---------- PUT /api/strategy 走 strategy_save ----------


async def test_put_strategy_via_strategy_save(repo: Repo, tmp_path: Path):
    """验证策略保存回调接线后端点透传全文并保持纯文本响应契约。

    参数：
        repo: Repo，临时数据库仓储
        tmp_path: Path，pytest 临时目录

    返回：
        None，通过断言验证回调收到原文且响应原样回显
    """
    saved: list[str] = []

    async def _save(content: str) -> dict:
        """记录待保存策略全文并返回固定版本编号。

        参数：
            content: str，端点收到的策略书全文

        返回：
            dict，表示保存成功且版本编号为 5
        """
        saved.append(content)
        return {"saved": True, "version": 5}

    deps = _deps(repo, tmp_path, strategy_save=_save)
    async with _client_of(deps) as c:
        body = "新策略书全文。" * 30
        r = await c.put("/api/strategy", content=body)
        assert r.status_code == 200 and r.text == body  # 响应原文回显（契约零破坏）
        assert saved == [body]  # 全文透传给 strategy_save


async def test_put_strategy_validation_error_maps_422(repo: Repo, tmp_path: Path):
    """验证策略校验错误被映射为含完整原因的 422 响应。

    参数：
        repo: Repo，临时数据库仓储
        tmp_path: Path，pytest 临时目录

    返回：
        None，通过断言验证状态码与过短原因
    """

    async def _reject(content: str) -> dict:
        """模拟策略正文过短的校验失败。

        参数：
            content: str，待保存策略正文，本桩不读取其内容

        返回：
            dict，本函数始终在返回前抛出异常

        异常：
            StrategyValidationError: 每次调用均携带策略过短原因抛出
        """
        raise StrategyValidationError(["策略书过短：strip 后 4 字符，最少 100 字符"])

    async with _client_of(_deps(repo, tmp_path, strategy_save=_reject)) as c:
        r = await c.put("/api/strategy", content="太短")
        assert r.status_code == 422
        assert "策略书过短" in r.json()["detail"]


async def test_put_strategy_no_diff_idempotent(repo: Repo, tmp_path: Path):
    """验证策略内容无差异时版本为空仍被视为幂等保存成功。

    参数：
        repo: Repo，临时数据库仓储
        tmp_path: Path，pytest 临时目录

    返回：
        None，通过断言验证成功状态和纯文本响应
    """

    async def _no_diff(content: str) -> dict:
        """模拟策略内容与当前版本无差异的幂等结果。

        参数：
            content: str，待保存策略正文，本桩不读取其内容

        返回：
            dict，表示保存成功但没有生成新版本
        """
        return {"saved": True, "version": None}

    async with _client_of(_deps(repo, tmp_path, strategy_save=_no_diff)) as c:
        body = "与当前一致的策略书。" * 20
        r = await c.put("/api/strategy", content=body)
        assert r.status_code == 200 and r.text == body


# ---------- PUT /api/config 的 review.* 热写回 ----------


async def test_put_config_review_keys_hot_applied(repo: Repo, tmp_path: Path):
    """验证复盘开关、时间和间隔配置热写回运行时且非法值被拒绝。

    参数：
        repo: Repo，临时数据库仓储
        tmp_path: Path，pytest 临时目录

    返回：
        None，通过断言验证无需重启、运行时同步及两个 422 边界
    """
    runtime = Settings()
    assert runtime.review.enabled is True
    deps = _deps(repo, tmp_path, runtime_settings=runtime)
    async with _client_of(deps) as c:
        r = await c.put(
            "/api/config",
            json={"review": {"enabled": False, "daily_time": "04:30", "interval_days": 3}},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["saved"] is True
        assert "review.enabled" not in body["needs_restart"]
        assert "review.daily_time" not in body["needs_restart"]
        assert "review.interval_days" not in body["needs_restart"]
        assert runtime.review.enabled is False  # 热写回（scheduler 下 tick 即生效）
        assert runtime.review.daily_time == "04:30"
        assert runtime.review.interval_days == 3
        # 非法 daily_time → 422
        r = await c.put("/api/config", json={"review": {"daily_time": "25:00"}})
        assert r.status_code == 422
        # 非法 interval_days（越界）→ 422，运行时值不变
        r = await c.put("/api/config", json={"review": {"interval_days": 0}})
        assert r.status_code == 422
        assert runtime.review.interval_days == 3


# ---------- POST /api/review/research/rereview（人工重评授权登记，R5-2） ----------


async def test_research_rereview_endpoint(repo: Repo, tmp_path: Path):
    """人工重评授权端点：404 目标不存在、409 未复盘、422 空理由、200 登记与幂等复用。

    参数：
        repo: Repo，临时数据库仓储
        tmp_path: Path，pytest 临时目录

    返回：
        None，通过断言验证四种状态码与幂等语义（重复登记返回既有授权同 id）
    """
    from tests.research_helpers import save_report_fixture

    report = await save_report_fixture(repo, report_type="us_open", horizon="当日")
    async with _client_of(_deps(repo, tmp_path)) as c:
        missing = await c.post(
            "/api/review/research/rereview",
            json={"report_id": 999999, "contract": "BTC_USDT", "reason": "复核"},
        )
        assert missing.status_code == 404

        not_reviewed = await c.post(
            "/api/review/research/rereview",
            json={"report_id": report.id, "contract": "BTC_USDT", "reason": "复核"},
        )
        assert not_reviewed.status_code == 409  # 未复盘目标由自动路径覆盖，无需授权

        empty_reason = await c.post(
            "/api/review/research/rereview",
            json={"report_id": report.id, "contract": "BTC_USDT", "reason": "  "},
        )
        assert empty_reason.status_code == 422

        await repo.research_review.save_review(
            review_report_id=1, report_id=report.id, contract="BTC_USDT"
        )
        created = await c.post(
            "/api/review/research/rereview",
            json={"report_id": report.id, "contract": "BTC_USDT", "reason": "原复盘误判"},
        )
        assert created.status_code == 200
        body = created.json()
        assert body["reused"] is False
        assert body["reason"] == "原复盘误判" and body["requested_by"] == "human"
        assert body["consumed_round_id"] == ""

        again = await c.post(
            "/api/review/research/rereview",
            json={"report_id": report.id, "contract": "BTC_USDT", "reason": "换个理由"},
        )
        assert again.status_code == 200
        body2 = again.json()
        assert body2["reused"] is True
        assert body2["id"] == body["id"] and body2["reason"] == "原复盘误判"  # 幂等命中不覆盖
