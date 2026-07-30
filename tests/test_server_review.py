"""复盘/策略版本端点行为测试：fake 依赖注入（tmp_path 隔离真实配置与 DB）。

覆盖：
- GET /api/review/reports(+{id})：列表截断 200、详情全文、404；
- POST /api/review/run：未接线 503、LLM 未配置 503、进行中 409、成功 200；
- GET /api/strategy/versions(+{id})：列表不含 content、详情含 content、404；
- GET /api/strategy/diff：200 纯文本、版本不存在 404、参数非法 422；
- POST /api/strategy/rollback/{id}：成功 200、版本不存在 404、未接线 503；
- PUT /api/strategy 走 strategy_save（响应保持 PlainText 原文、422 路径、无差异幂等路径）；
- PUT /api/config 的 review.enabled/daily_time/interval_days 热写回运行时（_RUNTIME_KEYS）。
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
    db = Database()
    await db.open(tmp_path / "t.db")
    yield Repo(db)
    await db.close()


def _deps(repo: Repo, tmp_path: Path, **overrides: Any) -> ServerDeps:
    """组装 fake 依赖：tmp 配置/策略书 + 指定回调覆盖。"""
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
    return AsyncClient(transport=ASGITransport(app=create_app(deps)), base_url="http://test")


# ---------- GET /api/review/reports(+{id}) ----------


async def test_review_reports_list_and_detail(repo: Repo, tmp_path: Path):
    await repo.review.save_review_report(1000.0, 2000.0, '{"a":1}', "短报告", "none")
    r2 = await repo.review.save_review_report(
        3000.0, 4000.0, '{"b":2}', _LONG_MD, "rewrite", new_version_id=7
    )
    async with _client_of(_deps(repo, tmp_path)) as c:
        body = (await c.get("/api/review/reports")).json()
        assert body["total"] == 2
        assert [i["id"] for i in body["items"]] == [r2.id, r2.id - 1]  # 最新在前
        item = body["items"][0]
        assert len(item["report_md"]) == 200  # 列表截断
        assert item["new_version_id"] == 7 and item["strategy_action"] == "rewrite"
        detail = (await c.get(f"/api/review/reports/{r2.id}")).json()
        assert detail["report_md"] == _LONG_MD  # 详情全文
        assert (await c.get("/api/review/reports/999")).status_code == 404


# ---------- POST /api/review/run ----------


async def test_review_run_status_mapping(repo: Repo, tmp_path: Path):
    """状态码映射走结构化 error_code（busy→409、llm_not_configured→503），不依赖错误文案。"""
    async with _client_of(_deps(repo, tmp_path)) as c:  # 未接线
        r = await c.post("/api/review/run")
        assert r.status_code == 503

    async def _busy() -> dict:
        return {"started": False, "error": "busy right now", "error_code": "busy"}

    async def _no_llm() -> dict:
        return {
            "started": False,
            "ok": False,
            "error": "no llm at all",
            "error_code": "llm_not_configured",
        }

    async def _ok() -> dict:
        return {"started": True, "ok": True, "report_id": 1}

    async with _client_of(_deps(repo, tmp_path, review_run=_busy)) as c:
        assert (await c.post("/api/review/run")).status_code == 409
    async with _client_of(_deps(repo, tmp_path, review_run=_no_llm)) as c:
        assert (await c.post("/api/review/run")).status_code == 503
    async with _client_of(_deps(repo, tmp_path, review_run=_ok)) as c:
        r = await c.post("/api/review/run")
        assert r.status_code == 200
        assert r.json()["started"] is True and r.json()["ok"] is True


async def test_review_run_with_explicit_period(repo: Repo, tmp_path: Path):
    """POST /api/review/run 接受可选 JSON body 透传补跑区间；非法输入 422。"""
    calls: list[dict] = []

    async def _run(**kwargs) -> dict:
        calls.append(kwargs)
        return {"started": True, "ok": True, "report_id": len(calls)}

    async def _invalid(**kwargs) -> dict:
        return {"started": False, "error": "区间非法", "error_code": "invalid_period"}

    async with _client_of(_deps(repo, tmp_path, review_run=_run)) as c:
        r = await c.post("/api/review/run", json={"start_ts": 1000.0, "end_ts": 2000.0})
        assert r.status_code == 200 and r.json()["report_id"] == 1
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
    async def _ok(version_id: int) -> dict:
        return {"rolled_back_to": version_id, "version": 3}

    async def _missing(version_id: int) -> dict:
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
    """接线后经 strategy_save 保存：响应契约保持 PlainText 原文不变。"""
    saved: list[str] = []

    async def _save(content: str) -> dict:
        saved.append(content)
        return {"saved": True, "version": 5}

    deps = _deps(repo, tmp_path, strategy_save=_save)
    async with _client_of(deps) as c:
        body = "新策略书全文。" * 30
        r = await c.put("/api/strategy", content=body)
        assert r.status_code == 200 and r.text == body  # 响应原文回显（契约零破坏）
        assert saved == [body]  # 全文透传给 strategy_save


async def test_put_strategy_validation_error_maps_422(repo: Repo, tmp_path: Path):
    """校验失败映 422，detail 为全部原因拼接；成功路径不受影响。"""

    async def _reject(content: str) -> dict:
        raise StrategyValidationError(["策略书过短：strip 后 4 字符，最少 100 字符"])

    async with _client_of(_deps(repo, tmp_path, strategy_save=_reject)) as c:
        r = await c.put("/api/strategy", content="太短")
        assert r.status_code == 422
        assert "策略书过短" in r.json()["detail"]


async def test_put_strategy_no_diff_idempotent(repo: Repo, tmp_path: Path):
    """无差异幂等路径：回调返回 version=None 视为保存成功（不产新版本）。"""

    async def _no_diff(content: str) -> dict:
        return {"saved": True, "version": None}

    async with _client_of(_deps(repo, tmp_path, strategy_save=_no_diff)) as c:
        body = "与当前一致的策略书。" * 20
        r = await c.put("/api/strategy", content=body)
        assert r.status_code == 200 and r.text == body


# ---------- PUT /api/config 的 review.* 热写回 ----------


async def test_put_config_review_keys_hot_applied(repo: Repo, tmp_path: Path):
    """review.enabled/daily_time/interval_days 属 _RUNTIME_KEYS：写回运行时实例，不进 needs_restart。"""
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
